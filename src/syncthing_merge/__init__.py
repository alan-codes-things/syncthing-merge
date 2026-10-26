"""Safely merge Syncthing ``.sync-conflict-*`` files.

The tool is deliberately conservative:

* live files are never passed to ``git merge-file`` or to a GUI merge tool;
* all merging happens on temporary ``LOCAL``/``BASE``/``REMOTE``/``MERGED``
  copies;
* every result is validated before it is installed;
* the current file and the conflict file are backed up before anything is
  replaced;
* installation is atomic.

If anything goes wrong the live file is left alone and the conflict file is
kept.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

VERSION = "0.2.0"

STAMP_FORMAT = "%Y%m%d-%H%M%S"
GIT_STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
TEMP_PREFIX = "~syncthing~"
DEFAULT_VERSIONS_DIR = ".stversions"

# Syncthing inserts the conflict tag before the final extension:
#   archive.tar.gz -> archive.tar.sync-conflict-20260101-120000-ABCDEFG.gz
# For a dotfile such as .env the stem is empty:
#   .env -> .sync-conflict-20260101-120000-ABCDEFG.env
CONFLICT_RE = re.compile(
    r"^(?P<stem>.*)\.sync-conflict-(?P<stamp>\d{8}-\d{6})-"
    r"(?P<device>[A-Za-z0-9]+)(?P<ext>\..*)?$",
    re.DOTALL,
)

# Git conflict markers start in column zero.  The base marker is included for
# diff3-style output.
MARKER_RE = re.compile(rb"^(?:<{7}|={7}|>{7}|\|{7})(?:[ \t].*)?$")

# Commands follow the argument order used by Git's own mergetools.  Keeping
# this table in one place makes the pane layout predictable:
#
#   LOCAL | BASE | REMOTE  ->  MERGED / Result / Output
#
# The user resolves conflicts in the MERGED pane, never in LOCAL/BASE/REMOTE.
THREE_WAY_TOOLS: dict[str, str] = {
    "meld": 'meld --auto-merge --output="$MERGED" "$LOCAL" "$BASE" "$REMOTE"',
    "vscode": 'code --wait --merge "$REMOTE" "$LOCAL" "$BASE" "$MERGED"',
    "code": 'code --wait --merge "$REMOTE" "$LOCAL" "$BASE" "$MERGED"',
    "kdiff3": (
        'kdiff3 --auto --L1 "$MERGED (Base)" --L2 "$MERGED (Local)" '
        '--L3 "$MERGED (Remote)" -o "$MERGED" "$BASE" "$LOCAL" "$REMOTE"'
    ),
    "p4merge": 'p4merge "$BASE" "$REMOTE" "$LOCAL" "$MERGED"',
    "bc": 'bcomp "$LOCAL" "$REMOTE" "$BASE" -mergeoutput="$MERGED"',
    "bc3": 'bcomp "$LOCAL" "$REMOTE" "$BASE" -mergeoutput="$MERGED"',
    "bc4": 'bcomp "$LOCAL" "$REMOTE" "$BASE" -mergeoutput="$MERGED"',
    "vimdiff": 'vim -f -d -c "wincmd J" "$LOCAL" "$BASE" "$REMOTE" "$MERGED"',
    "opendiff": 'opendiff "$LOCAL" "$REMOTE" -ancestor "$BASE" -merge "$MERGED"',
}

# Two-way mode is opt-in.  LOCAL is the copy that will be installed, so tools
# are ordered to make LOCAL the editable/right-hand pane where possible.
TWO_WAY_TOOLS: dict[str, str] = {
    "meld": 'meld "$LOCAL" "$REMOTE"',
    "vscode": 'code --wait --diff "$REMOTE" "$LOCAL"',
    "code": 'code --wait --diff "$REMOTE" "$LOCAL"',
    "kdiff3": 'kdiff3 "$REMOTE" "$LOCAL"',
    "p4merge": 'p4merge "$REMOTE" "$LOCAL"',
    "bc": 'bcomp "$REMOTE" "$LOCAL"',
    "bc3": 'bcomp "$REMOTE" "$LOCAL"',
    "bc4": 'bcomp "$REMOTE" "$LOCAL"',
    "vimdiff": 'vim -f -d "$REMOTE" "$LOCAL"',
    "opendiff": 'opendiff "$REMOTE" "$LOCAL"',
}

TOOL_EXECUTABLES: dict[str, tuple[str, ...]] = {
    "meld": ("meld",),
    "vscode": ("code",),
    "code": ("code",),
    "kdiff3": ("kdiff3",),
    "p4merge": ("p4merge",),
    "bc": ("bcomp", "bcompare"),
    "bc3": ("bcomp", "bcompare"),
    "bc4": ("bcomp", "bcompare"),
    "vimdiff": ("vim", "vimdiff"),
    "opendiff": ("opendiff",),
}

TOOL_SCAN_ORDER = ("meld", "vscode", "kdiff3", "bc", "p4merge", "vimdiff")

RESOLVED = "resolved"
UNRESOLVED = "unresolved"
SKIPPED = "skipped"
FAILED = "failed"


@dataclass(frozen=True)
class ParsedConflict:
    """The pieces encoded in a Syncthing conflict filename."""

    stem: str
    ext: str
    stamp: datetime | None
    device: str

    @property
    def original_name(self) -> str:
        return self.stem + self.ext


@dataclass(frozen=True)
class Conflict:
    """A conflict file plus the paths it belongs to."""

    path: Path
    root: Path
    parsed: ParsedConflict

    @property
    def original_path(self) -> Path:
        return self.path.parent / self.parsed.original_name

    @property
    def rel_path(self) -> Path:
        return self.path.relative_to(self.root)

    @property
    def original_rel_path(self) -> Path:
        return self.original_path.relative_to(self.root)


@dataclass(frozen=True)
class BaseSource:
    """A common ancestor loaded into memory."""

    data: bytes
    kind: str
    label: str


@dataclass
class AutoMergeResult:
    """Outcome of a silent ``git merge-file`` run."""

    status: str  # clean | conflict | error
    data: bytes = b""
    preview: bytes = b""
    message: str = ""


@dataclass
class RunStats:
    resolved: int = 0
    unresolved: int = 0
    skipped: int = 0
    failed: int = 0

    @property
    def remaining(self) -> int:
        return self.unresolved + self.skipped + self.failed


def split_name(name: str) -> tuple[str, str]:
    """Split a filename the way Go's ``filepath.Ext`` does.

    In particular ``.env`` is treated as an extension with an empty stem, and
    ``archive.tar.gz`` keeps ``.gz`` as the extension.  This matches
    Syncthing's conflict/version naming.
    """

    dot = name.rfind(".")
    if dot < 0:
        return name, ""
    return name[:dot], name[dot:]


def parse_stamp(value: str) -> datetime | None:
    try:
        # Syncthing writes local timestamps without a timezone.  Attach the
        # local timezone so comparisons are well-defined and ruff-friendly.
        return datetime.strptime(value, STAMP_FORMAT).astimezone()
    except ValueError:
        return None


def parse_conflict_name(name: str) -> ParsedConflict | None:
    match = CONFLICT_RE.match(name)
    if not match:
        return None
    stem = match.group("stem")
    ext = match.group("ext") or ""
    if not stem + ext:
        return None
    return ParsedConflict(
        stem=stem,
        ext=ext,
        stamp=parse_stamp(match.group("stamp")),
        device=match.group("device"),
    )


def parse_version_name(name: str, original_name: str) -> datetime | None:
    """Return the timestamp encoded in a Syncthing version filename.

    Both the current ``stem~stamp.ext`` form and the older
    ``original~stamp`` form are accepted.
    """

    stem, ext = split_name(original_name)
    current = re.compile(
        rf"^{re.escape(stem)}~(\d{{8}}-\d{{6}}){re.escape(ext)}$",
        re.DOTALL,
    )
    legacy = re.compile(
        rf"^{re.escape(original_name)}~(\d{{8}}-\d{{6}})$",
        re.DOTALL,
    )
    for pattern in (current, legacy):
        match = pattern.match(name)
        if match:
            return parse_stamp(match.group(1))
    return None


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def marker_lines(data: bytes) -> set[bytes]:
    markers: set[bytes] = set()
    for line in data.splitlines():
        line = line.rstrip(b"\r")
        if MARKER_RE.match(line):
            markers.add(line)
    return markers


def ensure_newline(data: bytes) -> bytes:
    if not data or data.endswith(b"\n"):
        return data
    return data + b"\n"


def synthetic_preview(local: bytes, remote: bytes) -> bytes:
    """A safe fallback preview when git merge-file cannot produce one."""

    return (
        b"<<<<<<< LOCAL\n"
        + ensure_newline(local)
        + b"=======\n"
        + ensure_newline(remote)
        + b">>>>>>> REMOTE\n"
    )


def validate_result(
    merged: bytes,
    *,
    local: bytes,
    base: bytes,
    remote: bytes,
    preview: bytes | None = None,
    require_change: bool = False,
    allow_input_markers: bool = False,
    force: bool = False,
) -> list[str]:
    """Return a list of reasons why *merged* must not be installed.

    ``force`` can relax the two "suspicious" checks (empty result and
    result-equals-base).  It never allows conflict markers or an unchanged
    conflicted preview.
    """

    problems: list[str] = []
    suspicious: list[str] = []

    if require_change and preview is not None and merged == preview:
        problems.append("the merge tool did not change the conflicted result")

    if not merged:
        local_deleted = local == b"" and (remote == base or remote == b"")
        remote_deleted = remote == b"" and (local == base or local == b"")
        if not (local_deleted or remote_deleted):
            suspicious.append("the result is empty even though the inputs are not")

    if merged == base and (local != base or remote != base):
        suspicious.append(
            "the result is identical to the base even though a side changed"
        )

    output_markers = marker_lines(merged)
    if output_markers:
        if not allow_input_markers:
            problems.append("the result still contains Git conflict markers")
        else:
            input_markers = (
                marker_lines(local) | marker_lines(base) | marker_lines(remote)
            )
            new_markers = output_markers - input_markers
            if new_markers:
                rendered = ", ".join(
                    sorted(line.decode("utf-8", "replace") for line in new_markers)
                )
                problems.append(f"the result contains new conflict markers: {rendered}")

    if is_binary(merged) and not (
        is_binary(local) or is_binary(base) or is_binary(remote)
    ):
        problems.append("the result contains NUL bytes even though the inputs are text")

    if not force:
        problems.extend(suspicious)
    return problems


def path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def default_backup_root() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            base = str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_STATE_HOME")
        if not base:
            base = str(Path.home() / ".local" / "state")
    return (Path(base) / "syncthing-merge" / "backups").expanduser().resolve()


def run_git(
    args: list[str], cwd: Path, *, text: bool = False
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=text,
        check=False,
    )


def git_config_get(key: str, cwd: Path) -> str:
    try:
        result = run_git(["config", "--get", key], cwd, text=True)
    except FileNotFoundError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def find_git_root(root: Path) -> Path | None:
    try:
        result = run_git(["rev-parse", "--show-toplevel"], root, text=True)
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return Path(value).resolve() if value else None


def git_dirty_files(git_root: Path) -> list[str]:
    try:
        result = run_git(
            ["status", "--porcelain", "--untracked-files=no"], git_root, text=True
        )
    except FileNotFoundError:
        return []
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def tool_executable(tool: str) -> str | None:
    for name in TOOL_EXECUTABLES.get(tool, ()):
        path = shutil.which(name)
        if path:
            return path
    return None


def detect_tool(requested: str | None, root: Path) -> str | None:
    if requested:
        return requested.lower()

    configured = git_config_get("merge.tool", root)
    if configured:
        return configured.lower()

    for name in TOOL_SCAN_ORDER:
        if tool_executable(name):
            return name
    return None


def adapt_builtin_executable(tool: str, template: str) -> str:
    """Use the executable that was actually found on PATH.

    Most tools have the same executable name as their Git tool name.  Beyond
    Compare installs ``bcomp`` on Linux and ``bcompare`` on macOS, and vimdiff
    may only provide the ``vimdiff`` wrapper, so adapt the first token.
    """

    executable = tool_executable(tool)
    if not executable:
        return template
    first, separator, rest = template.partition(" ")
    if not separator or Path(executable).stem == first:
        return template
    if os.name == "nt":
        replacement = Path(executable).name
    else:
        replacement = shlex.quote(executable)
    return replacement + " " + rest


def tool_command(tool: str, root: Path, *, two_way: bool = False) -> str | None:
    """Return the command template for *tool*.

    A user's Git configuration always wins, matching how ``git mergetool``
    behaves.  Built-in commands are only a fallback.
    """

    if two_way:
        configured = git_config_get(f"difftool.{tool}.cmd", root)
        if configured:
            return configured
        template = TWO_WAY_TOOLS.get(tool)
    else:
        configured = git_config_get(f"mergetool.{tool}.cmd", root)
        if configured:
            return configured
        template = THREE_WAY_TOOLS.get(tool)
    if template is None:
        return None
    return adapt_builtin_executable(tool, template)


def tool_is_configured(tool: str, root: Path, *, two_way: bool = False) -> bool:
    """True when the tool has either a custom command or a known executable."""

    key = f"difftool.{tool}.cmd" if two_way else f"mergetool.{tool}.cmd"
    if git_config_get(key, root):
        return True
    return tool_executable(tool) is not None


def run_tool(
    template: str,
    paths: dict[str, Path],
    *,
    cwd: Path,
) -> subprocess.CompletedProcess:
    """Execute a mergetool command with Git-compatible environment variables.

    Placeholders are expanded by the shell from environment variables, just
    like ``git mergetool`` does.  This avoids fragile manual quoting of paths
    that may contain spaces.
    """

    env = os.environ.copy()
    for name, value in paths.items():
        env[name] = str(value)

    command = template
    if os.name == "nt":
        for name in paths:
            command = command.replace(f"${{{name}}}", f"%{name}%")
            command = command.replace(f"${name}", f"%{name}%")
    else:
        for name in paths:
            command = command.replace(f"%{name}%", f"${name}")

    return subprocess.run(command, shell=True, cwd=cwd, env=env, check=False)


def install_atomic(target: Path, data: bytes, *, mode: int | None = None) -> None:
    """Write *data* to *target* without ever exposing a partial file."""

    if target.is_symlink():
        raise OSError(f"refusing to replace symlink: {target}")

    target.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            mode = stat.S_IMODE(target.stat().st_mode)
        except OSError:
            mode = 0o644

    fd, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=TEMP_PREFIX,
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def copy_backup(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise OSError(f"refusing to back up symlink: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def write_backup(destination: Path, data: bytes, *, mode: int = 0o644) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    install_atomic(destination, data, mode=mode)


class App:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.root = Path(args.path).expanduser().resolve()
        self.tool = detect_tool(args.tool, self.root)
        self.git_root: Path | None = None
        self.dirty: list[str] = []
        self.stats = RunStats()
        self.run_dir: Path | None = None
        self.backup_parent = self._resolve_backup_parent()
        self.no_backup_confirmed = False
        self.temp_dirs: list[Path] = []

    # ------------------------------------------------------------------
    # Configuration and output
    # ------------------------------------------------------------------

    def _resolve_backup_parent(self) -> Path | None:
        if self.args.no_backup:
            return None
        if self.args.backup_dir:
            return Path(self.args.backup_dir).expanduser().resolve()
        return default_backup_root()

    def _backup_run_parent(self) -> Path | None:
        if self.backup_parent is None:
            return None
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", self.root.name).strip(".-")
        if not slug:
            slug = "root"
        digest = hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:10]
        return self.backup_parent / f"{slug}-{digest}"

    def ensure_run_dir(self) -> Path | None:
        if self.args.no_backup:
            return None
        if self.run_dir is not None:
            return self.run_dir

        parent = self._backup_run_parent()
        if parent is None:
            return None
        parent.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().astimezone().strftime(STAMP_FORMAT)
        for attempt in range(1000):
            name = stamp if attempt == 0 else f"{stamp}-{attempt}"
            candidate = parent / name
            try:
                candidate.mkdir()
            except FileExistsError:
                continue
            self.run_dir = candidate
            self._write_manifest_header()
            return candidate
        raise OSError(f"could not create a unique backup directory under {parent}")

    def _write_manifest_header(self) -> None:
        if self.run_dir is None:
            return
        lines = [
            f"syncthing-merge {VERSION}",
            f"root: {self.root}",
            f"started: {datetime.now().astimezone():%Y-%m-%d %H:%M:%S}",
            f"tool: {self.tool or 'none'}",
            "",
        ]
        (self.run_dir / "MANIFEST.txt").write_text("\n".join(lines), encoding="utf-8")

    def append_manifest(self, text: str) -> None:
        if self.run_dir is None:
            return
        try:
            with (self.run_dir / "MANIFEST.txt").open("a", encoding="utf-8") as handle:
                handle.write(text)
                if not text.endswith("\n"):
                    handle.write("\n")
        except OSError as error:
            self.warn(f"could not update backup manifest: {error}")

    def log(self, message: str = "", *, verbose_only: bool = False) -> None:
        if verbose_only and not self.args.verbose:
            return
        print(message)

    def warn(self, message: str) -> None:
        print(f"[!] {message}")

    def error(self, message: str) -> None:
        print(f"[x] {message}", file=sys.stderr)

    def confirm_without_backup(self) -> bool:
        if not self.args.no_backup:
            return True
        if self.args.yes or self.no_backup_confirmed:
            return True
        if not sys.stdin.isatty():
            self.error("--no-backup requires --yes when standard input is not a TTY")
            return False
        answer = input("Backups are disabled. Continue without backups? [y/N] ")
        if answer.strip().lower() in {"y", "yes"}:
            self.no_backup_confirmed = True
            return True
        return False

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def detect_git(self) -> None:
        self.git_root = find_git_root(self.root)
        if self.git_root is not None:
            self.dirty = git_dirty_files(self.git_root)

    def _is_excluded(self, path: Path) -> bool:
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            return True
        relative_text = relative.as_posix()
        for pattern in self.args.exclude:
            if fnmatch.fnmatch(relative_text, pattern):
                return True
            if fnmatch.fnmatch(path.name, pattern):
                return True
            if any(fnmatch.fnmatch(part, pattern) for part in relative.parts):
                return True
        return False

    def scan_conflicts(self) -> list[Conflict]:
        versions_root = Path(self.args.versions_dir).expanduser()
        if not versions_root.is_absolute():
            versions_root = self.root / versions_root
        versions_root = versions_root.resolve()
        skip_names = {".git", ".stversions"}
        conflicts: list[Conflict] = []

        for directory, dirnames, filenames in os.walk(self.root, followlinks=False):
            current = Path(directory)
            kept_dirs: list[str] = []
            for name in dirnames:
                candidate = current / name
                if name in skip_names:
                    continue
                if self._is_excluded(candidate):
                    continue
                if path_is_relative_to(candidate, versions_root):
                    continue
                if (
                    self.backup_parent is not None
                    and path_is_relative_to(self.backup_parent, self.root)
                    and path_is_relative_to(candidate, self.backup_parent)
                ):
                    continue
                kept_dirs.append(name)
            dirnames[:] = kept_dirs

            for name in filenames:
                parsed = parse_conflict_name(name)
                if parsed is None:
                    continue
                path = current / name
                if self._is_excluded(path):
                    continue
                conflicts.append(Conflict(path=path, root=self.root, parsed=parsed))

        conflicts.sort(key=lambda item: item.rel_path.as_posix())
        return conflicts

    # ------------------------------------------------------------------
    # Common ancestors
    # ------------------------------------------------------------------

    def find_git_base(self, conflict: Conflict) -> BaseSource | None:
        if self.args.no_git_base or self.git_root is None:
            return None
        try:
            relative = conflict.original_path.relative_to(self.git_root).as_posix()
        except ValueError:
            return None

        tracked = run_git(
            ["ls-files", "--error-unmatch", "--", relative],
            self.git_root,
            text=True,
        )
        if tracked.returncode != 0:
            return None

        commit = ""
        if conflict.parsed.stamp is not None:
            before = conflict.parsed.stamp.strftime(GIT_STAMP_FORMAT)
            revision = run_git(
                ["rev-list", "-1", f"--before={before}", "HEAD", "--", relative],
                self.git_root,
                text=True,
            )
            if revision.returncode == 0:
                commit = revision.stdout.strip()
            if not commit:
                return None
        else:
            revision = run_git(["rev-parse", "HEAD"], self.git_root, text=True)
            if revision.returncode == 0:
                commit = revision.stdout.strip()
            if not commit:
                return None

        shown = run_git(
            ["show", f"{commit}:{relative}"],
            self.git_root,
        )
        if shown.returncode != 0:
            return None

        label = f"Git {commit[:12]}:{relative}"
        return BaseSource(data=shown.stdout, kind="git", label=label)

    def find_version_base(self, conflict: Conflict) -> BaseSource | None:
        if self.args.no_stversions:
            return None

        versions_root = Path(self.args.versions_dir).expanduser()
        if not versions_root.is_absolute():
            versions_root = self.root / versions_root
        candidate_dir = versions_root / conflict.original_rel_path.parent
        if not candidate_dir.is_dir():
            return None

        candidates: list[tuple[datetime, Path]] = []
        for entry in candidate_dir.iterdir():
            if entry.is_symlink() or not entry.is_file():
                continue
            stamp = parse_version_name(entry.name, conflict.parsed.original_name)
            if stamp is None:
                continue
            if conflict.parsed.stamp is not None and stamp > conflict.parsed.stamp:
                self.log(
                    f"    ignoring future version: {entry.name}",
                    verbose_only=True,
                )
                continue
            candidates.append((stamp, entry))

        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)
        stamp, path = candidates[0]
        try:
            data = path.read_bytes()
        except OSError as error:
            self.warn(f"could not read version {path}: {error}")
            return None

        try:
            shown_path = path.relative_to(self.root)
        except ValueError:
            shown_path = path
        label = f"Syncthing version {shown_path} ({stamp:%Y-%m-%d %H:%M:%S})"
        return BaseSource(data=data, kind="stversions", label=label)

    def resolve_base(self, conflict: Conflict) -> BaseSource:
        if self.args.empty_base:
            return BaseSource(b"", "empty", "empty base (--empty-base)")

        base = self.find_git_base(conflict)
        if base is None:
            base = self.find_version_base(conflict)
        if base is None:
            return BaseSource(b"", "empty", "empty base (no common ancestor)")
        return base

    # ------------------------------------------------------------------
    # Automatic merge
    # ------------------------------------------------------------------

    def auto_merge(
        self,
        conflict: Conflict,
        local: bytes,
        base: BaseSource,
        remote: bytes,
    ) -> AutoMergeResult:
        local_hash = sha256_file(conflict.original_path)
        remote_hash = sha256_file(conflict.path)

        with tempfile.TemporaryDirectory(prefix="syncthing-merge-auto-") as temporary:
            directory = Path(temporary)
            local_path = directory / "LOCAL"
            base_path = directory / "BASE"
            remote_path = directory / "REMOTE"
            local_path.write_bytes(local)
            base_path.write_bytes(base.data)
            remote_path.write_bytes(remote)

            try:
                result = subprocess.run(
                    [
                        "git",
                        "merge-file",
                        "-p",
                        "--diff3",
                        "-q",
                        "-L",
                        "LOCAL",
                        "-L",
                        "BASE",
                        "-L",
                        "REMOTE",
                        str(local_path),
                        str(base_path),
                        str(remote_path),
                    ],
                    capture_output=True,
                    check=False,
                )
            except FileNotFoundError:
                return AutoMergeResult(
                    status="error", message="git is not installed or not on PATH"
                )

        if (
            sha256_file(conflict.original_path) != local_hash
            or sha256_file(conflict.path) != remote_hash
        ):
            return AutoMergeResult(
                status="error",
                message="a live file changed while the automatic merge was running",
            )

        if result.returncode == 0:
            problems = validate_result(
                result.stdout,
                local=local,
                base=base.data,
                remote=remote,
                allow_input_markers=self.args.allow_input_markers,
                force=self.args.force,
            )
            if problems:
                return AutoMergeResult(
                    status="conflict",
                    data=result.stdout,
                    preview=result.stdout,
                    message="automatic result rejected: " + "; ".join(problems),
                )
            return AutoMergeResult(
                status="clean", data=result.stdout, preview=result.stdout
            )

        if 0 < result.returncode < 128:
            preview = result.stdout or synthetic_preview(local, remote)
            return AutoMergeResult(
                status="conflict",
                preview=preview,
                message=f"automatic merge found {result.returncode} conflict(s)",
            )

        stderr = result.stderr.decode("utf-8", "replace").strip()
        if not stderr:
            stderr = f"git merge-file exited with {result.returncode}"
        return AutoMergeResult(
            status="error",
            preview=result.stdout or synthetic_preview(local, remote),
            message=stderr,
        )

    # ------------------------------------------------------------------
    # Backups and installation
    # ------------------------------------------------------------------

    def backup_file(self, conflict: Conflict, source: Path, category: str) -> None:
        run_dir = self.ensure_run_dir()
        if run_dir is None:
            return
        relative = source.relative_to(self.root)
        copy_backup(source, run_dir / category / relative)

    def backup_data(
        self,
        conflict: Conflict,
        data: bytes,
        category: str,
        *,
        mode: int = 0o644,
    ) -> None:
        run_dir = self.ensure_run_dir()
        if run_dir is None:
            return
        destination = run_dir / category / conflict.original_rel_path
        write_backup(destination, data, mode=mode)

    def backup_before_tool(self, conflict: Conflict, base: BaseSource) -> bool:
        if self.args.no_backup:
            return True
        try:
            self.backup_file(conflict, conflict.original_path, "before")
            self.backup_file(conflict, conflict.path, "conflicts")
            if base.kind != "empty":
                self.backup_data(conflict, base.data, "base")
        except OSError as error:
            self.error(f"backup failed; refusing to open the merge tool: {error}")
            return False
        return True

    def backup_install(
        self,
        conflict: Conflict,
        base: BaseSource,
        merged: bytes,
        *,
        original_exists: bool = True,
        restored: bool = False,
    ) -> bool:
        if self.args.no_backup:
            return True
        try:
            if original_exists:
                self.backup_file(conflict, conflict.original_path, "before")
            self.backup_file(conflict, conflict.path, "conflicts")
            if base.kind != "empty":
                self.backup_data(conflict, base.data, "base")
            self.backup_data(
                conflict,
                merged,
                "restored" if restored else "merged",
            )
        except OSError as error:
            self.error(f"backup failed; refusing to modify the live file: {error}")
            return False
        return True

    def manifest_entry(
        self,
        conflict: Conflict,
        base: BaseSource,
        merged: bytes,
        *,
        method: str,
        restored: bool = False,
    ) -> None:
        lines = [
            f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S} {conflict.rel_path}",
            f"  base: {base.label}",
            f"  method: {method}",
            f"  sha256: {sha256_bytes(merged)}",
        ]
        if self.run_dir is not None:
            lines.append(f"  backup: {self.run_dir}")
        if restored:
            lines.append("  restored missing original")
        if self.args.keep_conflicts:
            lines.append("  conflict file kept")
        self.append_manifest("\n".join(lines))

    def _install_inputs_unchanged(
        self,
        conflict: Conflict,
        *,
        expected_original_hash: str | None,
        expected_conflict_hash: str | None,
        expect_missing: bool,
    ) -> bool:
        """Check that the live inputs still match what was merged."""

        if expect_missing:
            if conflict.original_path.exists():
                self.error(
                    "the original file appeared while merging; refusing to overwrite it"
                )
                return False
        elif (
            expected_original_hash is not None
            and sha256_file(conflict.original_path) != expected_original_hash
        ):
            self.error(
                "the original file changed while merging; refusing to overwrite it"
            )
            return False

        if (
            expected_conflict_hash is not None
            and sha256_file(conflict.path) != expected_conflict_hash
        ):
            self.error("the conflict file changed while merging; refusing to remove it")
            return False
        return True

    def finish_install(
        self,
        conflict: Conflict,
        merged: bytes,
        base: BaseSource,
        *,
        method: str,
        original_exists: bool = True,
        restored: bool = False,
        expected_original_hash: str | None = None,
        expected_conflict_hash: str | None = None,
    ) -> str:
        if self.args.dry_run:
            action = "restore missing original" if restored else "install merged file"
            print(f"    [dry-run] would {action} and remove conflict file")
            return RESOLVED

        if not self._install_inputs_unchanged(
            conflict,
            expected_original_hash=expected_original_hash,
            expected_conflict_hash=expected_conflict_hash,
            expect_missing=not original_exists,
        ):
            return FAILED

        if not self.confirm_without_backup():
            return SKIPPED

        if not self.backup_install(
            conflict,
            base,
            merged,
            original_exists=original_exists,
            restored=restored,
        ):
            return FAILED

        # Backups can take a while on large files.  Check again immediately
        # before replacing anything, so a Syncthing update during the backup
        # cannot be silently overwritten.
        if not self._install_inputs_unchanged(
            conflict,
            expected_original_hash=expected_original_hash,
            expected_conflict_hash=expected_conflict_hash,
            expect_missing=not original_exists,
        ):
            return FAILED

        if original_exists:
            try:
                mode = stat.S_IMODE(conflict.original_path.stat().st_mode)
            except OSError:
                mode = 0o644
        else:
            try:
                mode = stat.S_IMODE(conflict.path.stat().st_mode)
            except OSError:
                mode = 0o644

        try:
            install_atomic(conflict.original_path, merged, mode=mode)
        except OSError as error:
            self.error(f"could not install the result: {error}")
            return FAILED

        conflict_removed = self.args.keep_conflicts
        if self.args.keep_conflicts:
            print("    kept conflict file (--keep-conflicts)")
        elif (
            expected_conflict_hash is not None
            and sha256_file(conflict.path) != expected_conflict_hash
        ):
            self.error("the conflict file changed while merging; leaving it in place")
        else:
            try:
                conflict.path.unlink()
                conflict_removed = True
            except OSError as error:
                self.error(
                    "the merged file was installed, but the conflict file "
                    f"could not be removed: {error}"
                )

        self.manifest_entry(
            conflict,
            base,
            merged,
            method=method,
            restored=restored,
        )
        action = "restored missing original" if restored else "installed merged file"
        print(f"    [ok] {action}")
        return RESOLVED if conflict_removed else FAILED

    def restore_missing(self, conflict: Conflict) -> str:
        if conflict.path.is_symlink():
            self.warn("conflict file is a symlink; refusing to touch it")
            return SKIPPED
        try:
            data = conflict.path.read_bytes()
        except OSError as error:
            self.error(f"could not read conflict file: {error}")
            return FAILED

        problems = validate_result(
            data,
            local=data,
            base=b"",
            remote=data,
            allow_input_markers=self.args.allow_input_markers,
            force=self.args.force,
        )
        if problems:
            for problem in problems:
                self.error(problem)
            self.error("refusing to restore this conflict file")
            return FAILED

        print("    original is missing; restoring it from the conflict file")
        base = BaseSource(b"", "empty", "restore missing original")
        return self.finish_install(
            conflict,
            data,
            base,
            method="restore missing original",
            original_exists=False,
            restored=True,
            expected_conflict_hash=sha256_bytes(data),
        )

    # ------------------------------------------------------------------
    # Temporary directories
    # ------------------------------------------------------------------

    def cleanup_temp(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)
        try:
            self.temp_dirs.remove(path)
        except ValueError:
            pass

    def keep_temp(self, path: Path) -> None:
        self.error(f"temporary merge files kept for recovery: {path}")

    # ------------------------------------------------------------------
    # Visual merging
    # ------------------------------------------------------------------

    def _live_files_changed(
        self, conflict: Conflict, hashes: dict[Path, str | None]
    ) -> bool:
        for path, old_hash in hashes.items():
            if sha256_file(path) != old_hash:
                self.error(
                    "a live file changed unexpectedly while the merge tool was "
                    f"open: {path}"
                )
                return True
        return False

    def _restore_live_files(self, conflict: Conflict) -> None:
        if self.run_dir is None:
            self.warn("no backup is available to restore the live files")
            return
        entries = (
            (conflict.original_path, "before"),
            (conflict.path, "conflicts"),
        )
        for path, category in entries:
            backup = self.run_dir / category / path.relative_to(self.root)
            if not backup.exists():
                continue
            try:
                if not path.exists() or not path.is_file():
                    self.warn(
                        f"{path} is missing; not restoring it automatically "
                        f"(backup: {backup})"
                    )
                    continue
                current = path.read_bytes()
                if not marker_lines(current):
                    self.warn(
                        f"{path} changed externally; leaving it alone "
                        f"(backup: {backup})"
                    )
                    continue
                data = backup.read_bytes()
                mode = stat.S_IMODE(backup.stat().st_mode)
                install_atomic(path, data, mode=mode)
                self.warn(f"restored {path} from backup")
            except OSError as error:
                self.error(f"could not restore {path}: {error}")

    def visual_merge(
        self,
        conflict: Conflict,
        local: bytes,
        base: BaseSource,
        remote: bytes,
        preview: bytes,
    ) -> str:
        if self.args.no_open:
            print("    unresolved: visual tools are disabled; conflict preserved")
            return UNRESOLVED
        if not self.tool:
            print("    unresolved: no visual merge tool detected; conflict preserved")
            return UNRESOLVED

        template = tool_command(self.tool, self.root)
        if not template:
            print(
                f"    unresolved: no 3-way command is known for tool "
                f"'{self.tool}'; conflict preserved"
            )
            return UNRESOLVED
        if not tool_is_configured(self.tool, self.root):
            print(
                f"    unresolved: tool '{self.tool}' is not installed; "
                "conflict preserved"
            )
            return UNRESOLVED
        if self.args.dry_run:
            print(f"    [dry-run] would open {self.tool} to resolve the conflict")
            return UNRESOLVED

        if not self.confirm_without_backup():
            return SKIPPED
        if not self.backup_before_tool(conflict, base):
            return FAILED

        temporary = Path(tempfile.mkdtemp(prefix="syncthing-merge-gui-"))
        self.temp_dirs.append(temporary)
        local_path = temporary / "LOCAL"
        base_path = temporary / "BASE"
        remote_path = temporary / "REMOTE"
        merged_path = temporary / "MERGED"
        local_path.write_bytes(local)
        base_path.write_bytes(base.data)
        remote_path.write_bytes(remote)
        merged_path.write_bytes(preview)

        source_hashes = {
            local_path: sha256_bytes(local),
            base_path: sha256_bytes(base.data),
            remote_path: sha256_bytes(remote),
        }
        live_hashes = {
            conflict.original_path: sha256_file(conflict.original_path),
            conflict.path: sha256_file(conflict.path),
        }

        print(f"    opening {self.tool} ...")
        print("    resolve in the MERGED / Result / Output pane, then save and close.")
        self.log(f"    command: {template}", verbose_only=True)

        try:
            result = run_tool(
                template,
                {
                    "LOCAL": local_path,
                    "BASE": base_path,
                    "REMOTE": remote_path,
                    "MERGED": merged_path,
                },
                cwd=self.root,
            )
        except OSError as error:
            self.error(f"could not run {self.tool}: {error}")
            self.keep_temp(temporary)
            return FAILED

        if self._live_files_changed(conflict, live_hashes):
            self._restore_live_files(conflict)
            self.keep_temp(temporary)
            return FAILED

        for path, old_hash in source_hashes.items():
            if sha256_file(path) != old_hash:
                self.error(f"the merge tool modified {path.name}; refusing its result")
                self.keep_temp(temporary)
                return FAILED

        if result.returncode != 0:
            if result.returncode == 127:
                self.error(f"could not run {self.tool}: command not found")
            else:
                self.error(
                    f"{self.tool} exited with code {result.returncode}; "
                    "conflict preserved"
                )
            self.keep_temp(temporary)
            return FAILED

        try:
            merged = merged_path.read_bytes()
        except OSError as error:
            self.error(f"could not read the MERGED result: {error}")
            self.keep_temp(temporary)
            return FAILED

        problems = validate_result(
            merged,
            local=local,
            base=base.data,
            remote=remote,
            preview=preview,
            require_change=True,
            allow_input_markers=self.args.allow_input_markers,
            force=self.args.force,
        )
        if problems:
            for problem in problems:
                self.error(problem)
            self.error("refusing to install the result; the live file was not changed")
            self.keep_temp(temporary)
            return FAILED

        status = self.finish_install(
            conflict,
            merged,
            base,
            method=f"{self.tool} (3-way)",
            expected_original_hash=sha256_bytes(local),
            expected_conflict_hash=sha256_bytes(remote),
        )
        if status == RESOLVED:
            self.cleanup_temp(temporary)
        else:
            self.keep_temp(temporary)
        return status

    def two_way_merge(
        self,
        conflict: Conflict,
        local: bytes,
        remote: bytes,
    ) -> str:
        if self.args.no_open:
            print("    unresolved: visual tools are disabled; conflict preserved")
            return UNRESOLVED
        if not self.tool:
            print("    unresolved: no visual merge tool detected; conflict preserved")
            return UNRESOLVED

        template = tool_command(self.tool, self.root, two_way=True)
        if not template:
            print(
                f"    unresolved: no 2-way command is known for tool "
                f"'{self.tool}'; conflict preserved"
            )
            return UNRESOLVED
        if not tool_is_configured(self.tool, self.root, two_way=True):
            print(
                f"    unresolved: tool '{self.tool}' is not installed; "
                "conflict preserved"
            )
            return UNRESOLVED
        if self.args.dry_run:
            print(f"    [dry-run] would open {self.tool} in 2-way mode")
            return UNRESOLVED

        if not self.confirm_without_backup():
            return SKIPPED
        base = BaseSource(b"", "empty", "2-way mode")
        if not self.backup_before_tool(conflict, base):
            return FAILED

        temporary = Path(tempfile.mkdtemp(prefix="syncthing-merge-gui-"))
        self.temp_dirs.append(temporary)
        local_path = temporary / "LOCAL"
        base_path = temporary / "BASE"
        remote_path = temporary / "REMOTE"
        local_path.write_bytes(local)
        base_path.write_bytes(b"")
        remote_path.write_bytes(remote)

        remote_hash = sha256_bytes(remote)
        base_hash = sha256_bytes(b"")
        live_hashes = {
            conflict.original_path: sha256_file(conflict.original_path),
            conflict.path: sha256_file(conflict.path),
        }

        print(f"    opening {self.tool} in 2-way mode ...")
        print(
            "    2-way mode: edit the LOCAL pane (the copy that will be installed),"
            " save, and close."
        )
        print("    Do not edit REMOTE; only LOCAL is installed.")
        self.log(f"    command: {template}", verbose_only=True)

        try:
            result = run_tool(
                template,
                {
                    "LOCAL": local_path,
                    "BASE": base_path,
                    "REMOTE": remote_path,
                    "MERGED": local_path,
                },
                cwd=self.root,
            )
        except OSError as error:
            self.error(f"could not run {self.tool}: {error}")
            self.keep_temp(temporary)
            return FAILED

        if self._live_files_changed(conflict, live_hashes):
            self._restore_live_files(conflict)
            self.keep_temp(temporary)
            return FAILED

        if sha256_file(remote_path) != remote_hash:
            self.error("the merge tool modified REMOTE; 2-way mode only installs LOCAL")
            self.keep_temp(temporary)
            return FAILED
        if sha256_file(base_path) != base_hash:
            self.error("the merge tool modified BASE; refusing its result")
            self.keep_temp(temporary)
            return FAILED

        if result.returncode != 0:
            if result.returncode == 127:
                self.error(f"could not run {self.tool}: command not found")
            else:
                self.error(
                    f"{self.tool} exited with code {result.returncode}; "
                    "conflict preserved"
                )
            self.keep_temp(temporary)
            return FAILED

        try:
            merged = local_path.read_bytes()
        except OSError as error:
            self.error(f"could not read the LOCAL result: {error}")
            self.keep_temp(temporary)
            return FAILED

        if merged == local:
            self.error(
                "LOCAL was not modified; refusing to install it. "
                "If you meant to keep LOCAL, use the default 3-way mode and "
                "choose LOCAL in the MERGED pane."
            )
            self.keep_temp(temporary)
            return FAILED

        problems = validate_result(
            merged,
            local=local,
            base=b"",
            remote=remote,
            allow_input_markers=self.args.allow_input_markers,
            force=self.args.force,
        )
        if problems:
            for problem in problems:
                self.error(problem)
            self.error("refusing to install the result; the live file was not changed")
            self.keep_temp(temporary)
            return FAILED

        status = self.finish_install(
            conflict,
            merged,
            base,
            method=f"{self.tool} (2-way)",
            expected_original_hash=sha256_bytes(local),
            expected_conflict_hash=sha256_bytes(remote),
        )
        if status == RESOLVED:
            self.cleanup_temp(temporary)
        else:
            self.keep_temp(temporary)
        return status

    def merge_preview(self, preview: bytes, local: bytes, remote: bytes) -> bytes:
        """Return a preview that actually shows the overlapping changes."""

        if preview and marker_lines(preview):
            return preview
        return synthetic_preview(local, remote)

    def make_preview(
        self,
        conflict: Conflict,
        local: bytes,
        base: BaseSource,
        remote: bytes,
    ) -> bytes:
        result = self.auto_merge(conflict, local, base, remote)
        return self.merge_preview(result.preview, local, remote)

    # ------------------------------------------------------------------
    # Per-conflict processing
    # ------------------------------------------------------------------

    def process(self, conflict: Conflict) -> str:
        print(f"\n[+] {conflict.rel_path}")

        if conflict.path.is_symlink():
            self.warn("conflict file is a symlink; refusing to touch it")
            return SKIPPED
        if not conflict.path.is_file():
            self.warn("conflict path is not a regular file")
            return SKIPPED

        original = conflict.original_path
        if original.is_symlink():
            self.warn("original path is a symlink; refusing to touch it")
            return SKIPPED
        if not original.exists():
            if self.args.restore_missing:
                return self.restore_missing(conflict)
            self.warn(
                "original file is missing; conflict preserved. "
                "Use --restore-missing to restore it from the conflict file."
            )
            return SKIPPED
        if not original.is_file():
            self.warn("original path is not a regular file")
            return SKIPPED

        try:
            local = original.read_bytes()
            remote = conflict.path.read_bytes()
        except OSError as error:
            self.error(f"could not read files: {error}")
            return FAILED

        if is_binary(local) or is_binary(remote):
            self.warn(
                "binary conflict; automatic merging is disabled. "
                "Resolve it manually; both files were left untouched."
            )
            return SKIPPED

        if self.args.two_way:
            return self.two_way_merge(conflict, local, remote)

        base = self.resolve_base(conflict)
        if base.kind == "empty":
            print("    base: empty (no common ancestor)")
        else:
            print(f"    base: {base.label}")

        if base.kind != "empty" and is_binary(base.data):
            self.warn(
                "the common ancestor is binary; refusing automatic merging. "
                "Resolve it manually; both files were left untouched."
            )
            return SKIPPED

        if self.args.no_auto_merge:
            print("    automatic merge disabled (--no-auto-merge)")
            preview = self.make_preview(conflict, local, base, remote)
            return self.visual_merge(conflict, local, base, remote, preview)

        automatic = self.auto_merge(conflict, local, base, remote)
        if automatic.status == "clean":
            print("    automatic merge: clean")
            return self.finish_install(
                conflict,
                automatic.data,
                base,
                method="automatic (3-way)",
                expected_original_hash=sha256_bytes(local),
                expected_conflict_hash=sha256_bytes(remote),
            )

        if automatic.status == "conflict":
            print(f"    {automatic.message}")
        else:
            self.error(automatic.message)
        preview = self.merge_preview(automatic.preview, local, remote)
        return self.visual_merge(conflict, local, base, remote, preview)

    # ------------------------------------------------------------------
    # Reporting and top-level flow
    # ------------------------------------------------------------------

    def record(self, status: str) -> None:
        if status == RESOLVED:
            self.stats.resolved += 1
        elif status == UNRESOLVED:
            self.stats.unresolved += 1
        elif status == SKIPPED:
            self.stats.skipped += 1
        else:
            self.stats.failed += 1

    def fallback_tool(self) -> None:
        if not self.tool or self.args.tool:
            return
        if tool_is_configured(self.tool, self.root):
            return
        fallback = next(
            (
                name
                for name in TOOL_SCAN_ORDER
                if name != self.tool and tool_is_configured(name, self.root)
            ),
            None,
        )
        if fallback is not None:
            self.warn(
                f"configured merge tool '{self.tool}' is unavailable; "
                f"using '{fallback}' instead"
            )
            self.tool = fallback

    def print_banner(self, conflict_count: int) -> None:
        print(f"\nsyncthing-merge {VERSION}")
        print(f"Sync root: {self.root}")

        if self.tool:
            custom = git_config_get(f"mergetool.{self.tool}.cmd", self.root)
            executable = tool_executable(self.tool)
            if custom:
                print(f"Merge tool: {self.tool} (custom Git command)")
            elif executable:
                print(f"Merge tool: {self.tool} ({executable})")
            else:
                print(f"Merge tool: {self.tool} (executable not found)")
        else:
            print("Merge tool: none detected")

        if self.args.no_backup:
            print("[!] Backups are DISABLED (--no-backup).")
        else:
            print(f"Backups: {self._backup_run_parent()}")
            if self.backup_parent is not None and path_is_relative_to(
                self.backup_parent, self.root
            ):
                print(
                    "[!] The backup directory is inside the sync root; "
                    "Syncthing may sync the backups too."
                )

        if self.git_root is None:
            print("Git: not a worktree; Git history will not be used as a base.")
        elif self.dirty:
            print()
            print("[!] WARNING: Git has uncommitted tracked changes.")
            print("    Commit or stash them before merging so you can recover.")
            for line in self.dirty[:12]:
                print(f"      {line}")
            if len(self.dirty) > 12:
                print(f"      ... and {len(self.dirty) - 12} more")
            print("    Use --require-clean to abort instead of continuing.")
        else:
            print("Git: worktree is clean (tracked files).")

        print()
        print(f"Found {conflict_count} conflict file(s).")
        if self.args.dry_run:
            print("[dry-run] No files will be modified.")
        if self.args.auto or self.args.no_open:
            print("[auto] Visual merge tools are disabled for this run.")
        print(
            "Live files are only replaced after validation; conflicts are kept "
            "on any error."
        )

    def print_summary(self) -> None:
        print("\nSummary")
        print(f"  resolved:   {self.stats.resolved}")
        print(f"  unresolved: {self.stats.unresolved}")
        print(f"  skipped:    {self.stats.skipped}")
        print(f"  failed:     {self.stats.failed}")
        if self.run_dir is not None:
            print(f"  backups:    {self.run_dir}")
        if self.temp_dirs:
            print("  temporary files kept for recovery:")
            for path in self.temp_dirs:
                print(f"    {path}")
        if self.args.dry_run:
            print("\nDry run complete; no files were changed.")
        elif self.stats.remaining:
            print("\nSome conflicts remain. Resolve them and re-run the tool.")
        else:
            print("\nAll conflicts resolved.")

    def list_conflicts(self, conflicts: list[Conflict]) -> None:
        if not conflicts:
            print(f"No conflict files found in {self.root}.")
            return
        print(f"Found {len(conflicts)} conflict file(s) in {self.root}:")
        for conflict in conflicts:
            details: list[str] = []
            if conflict.parsed.stamp is not None:
                details.append(f"stamp={conflict.parsed.stamp:%Y-%m-%d %H:%M:%S}")
            details.append(f"device={conflict.parsed.device}")
            if conflict.original_path.is_symlink():
                details.append("original=symlink")
            elif not conflict.original_path.exists():
                details.append("original=MISSING")
            else:
                details.append(f"original={conflict.original_rel_path}")
            print(f"  {conflict.rel_path}")
            print(f"    {'  '.join(details)}")

    def doctor(self) -> int:
        print(f"\nsyncthing-merge {VERSION} doctor")
        problems: list[str] = []

        print(f"Python: {sys.version.split()[0]} ({sys.executable})")

        git = shutil.which("git")
        print(f"Git: {git or 'NOT FOUND'}")
        if git is None:
            problems.append("git is not installed")

        print(f"Sync root: {self.root}")
        if self.git_root is not None:
            print(f"Git root: {self.git_root}")
            if self.dirty:
                print(f"Git status: DIRTY ({len(self.dirty)} tracked change(s))")
                for line in self.dirty[:10]:
                    print(f"  {line}")
            else:
                print("Git status: clean (tracked files)")
        else:
            print("Git root: none (not a worktree)")

        print(f"Tool: {self.tool or 'none'}")
        if self.tool:
            command = tool_command(self.tool, self.root)
            print(f"3-way command: {command or 'NOT FOUND'}")
            if command is None:
                problems.append(f"no 3-way command for tool '{self.tool}'")
            custom = git_config_get(f"mergetool.{self.tool}.cmd", self.root)
            executable = tool_executable(self.tool)
            if custom:
                print("Executable: custom Git command")
            elif executable:
                print(f"Executable: {executable}")
            else:
                print("Executable: NOT FOUND")
                problems.append(f"tool '{self.tool}' executable not found")
        elif not self.args.no_open:
            problems.append("no merge tool detected; only automatic merges will work")

        if self.args.no_backup:
            print("Backups: DISABLED")
        else:
            parent = self._backup_run_parent()
            print(f"Backups: {parent}")
            if parent is not None:
                ancestor = parent
                while not ancestor.exists() and ancestor != ancestor.parent:
                    ancestor = ancestor.parent
                if not os.access(ancestor, os.W_OK):
                    problems.append(f"backup directory is not writable: {ancestor}")

        if sys.platform.startswith("linux"):
            display = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
            print(f"Display: {display or 'not set'}")
            if display is None and self.tool and not self.args.no_open:
                print("  (GUI tools may fail without DISPLAY/WAYLAND_DISPLAY)")

        conflicts = self.scan_conflicts()
        print(f"Conflicts: {len(conflicts)}")
        for conflict in conflicts[:20]:
            print(f"  {conflict.rel_path}")
        if len(conflicts) > 20:
            print(f"  ... and {len(conflicts) - 20} more")

        if problems:
            print("\nProblems:")
            for problem in problems:
                print(f"  [x] {problem}")
            return 1
        print("\nNo problems detected.")
        return 0

    def run(self) -> int:
        if not self.root.exists():
            self.error(f"path does not exist: {self.root}")
            return 1
        if not self.root.is_dir():
            self.error(f"path is not a directory: {self.root}")
            return 1
        if self.backup_parent is not None and self.backup_parent == self.root:
            self.error("--backup-dir cannot be the sync root itself")
            return 1

        self.detect_git()

        if self.args.doctor:
            return self.doctor()

        conflicts = self.scan_conflicts()
        if self.args.list:
            self.list_conflicts(conflicts)
            return 0

        if self.args.require_clean and self.dirty:
            self.error(
                "Git worktree has uncommitted tracked changes (--require-clean):"
            )
            for line in self.dirty:
                print(f"  {line}", file=sys.stderr)
            return 1

        self.fallback_tool()
        self.print_banner(len(conflicts))
        if not conflicts:
            print("No conflict files found.")
            return 0

        for conflict in conflicts:
            try:
                status = self.process(conflict)
            except Exception as error:  # noqa: BLE001 - keep processing other files
                self.error(f"unexpected error for {conflict.rel_path}: {error}")
                if self.args.verbose:
                    traceback.print_exc()
                status = FAILED
            self.record(status)

        self.print_summary()
        if self.args.dry_run:
            return 0
        return 0 if self.stats.remaining == 0 else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="syncthing-merge",
        description=(
            "Safely merge Syncthing .sync-conflict files. "
            "Live files are never passed to the merge engine or GUI tool."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  syncthing-merge                 # scan cwd, auto-merge, open tools\n"
            "  syncthing-merge --auto          # automatic merges only\n"
            "  syncthing-merge --dry-run       # show what would happen\n"
            "  syncthing-merge --list          # list conflicts\n"
            "  syncthing-merge --doctor        # check Git/tool/backup setup\n"
            "  syncthing-merge --require-clean # refuse to run on a dirty repo\n"
            "\n"
            "Resolve conflicts in the MERGED / Result / Output pane. "
            "The LOCAL, BASE, and REMOTE panes are read-only inputs.\n"
            "\n"
            "Exit codes: 0 = all resolved, 2 = some conflicts remain, "
            "1 = fatal error.\n"
        ),
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="sync root to scan (default: current directory)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    parser.add_argument(
        "--tool",
        metavar="NAME",
        help="Git mergetool name (default: read merge.tool from Git config)",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="only automatic merges; never open a visual tool (same as --no-open)",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="assume yes; never prompt (required with --no-backup in scripts)",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="never open a visual merge tool",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would happen without modifying anything",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list conflicts and exit",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="check Git, merge tool, display, and backup setup",
    )
    parser.add_argument(
        "--require-clean",
        action="store_true",
        help="abort if the Git worktree has uncommitted tracked changes",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow suspicious empty/base-equal results (never conflict markers)",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="disable backups (dangerous; requires --yes in scripts)",
    )
    parser.add_argument(
        "--backup-dir",
        metavar="PATH",
        help="parent directory for backups (default: user state directory)",
    )
    parser.add_argument(
        "--keep-conflicts",
        action="store_true",
        help="keep .sync-conflict files after resolving them",
    )
    parser.add_argument(
        "--restore-missing",
        action="store_true",
        help="restore a missing original from its conflict file",
    )
    parser.add_argument(
        "--two-way",
        action="store_true",
        help="force 2-way mode (edit the LOCAL copy; advanced)",
    )
    parser.add_argument(
        "--empty-base",
        action="store_true",
        help="use an empty 3-way base instead of Git/.stversions history",
    )
    parser.add_argument(
        "--no-auto-merge",
        action="store_true",
        help="skip git merge-file and open the visual tool directly",
    )
    parser.add_argument(
        "--no-stversions",
        action="store_true",
        help="do not use .stversions history as a base",
    )
    parser.add_argument(
        "--no-git-base",
        action="store_true",
        help="do not use Git history as a base",
    )
    parser.add_argument(
        "--allow-input-markers",
        action="store_true",
        help="allow conflict-marker lines that already exist in the inputs",
    )
    parser.add_argument(
        "--versions-dir",
        default=DEFAULT_VERSIONS_DIR,
        metavar="NAME",
        help="Syncthing versions directory (default: .stversions)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="skip paths matching GLOB (repeatable)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="show base selection and tool commands",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.auto:
        args.no_open = True
    if args.no_backup and args.backup_dir:
        parser.error("--no-backup cannot be combined with --backup-dir")
    if args.two_way and args.empty_base:
        parser.error("--two-way and --empty-base are mutually exclusive")

    try:
        app = App(args)
        return app.run()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as error:  # noqa: BLE001 - top-level CLI boundary
        print(f"[x] unexpected error: {error}", file=sys.stderr)
        if args.verbose:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
