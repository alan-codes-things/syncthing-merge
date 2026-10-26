from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

import syncthing_merge as sm  # noqa: I001


FAKE_TOOL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import os
    import sys
    from pathlib import Path

    record_path = os.environ.get("FAKE_RECORD")
    if record_path:
        def content(name):
            path = os.environ.get(name)
            if not path or not os.path.exists(path):
                return None
            return Path(path).read_text(errors="replace")

        Path(record_path).write_text(
            json.dumps(
                {
                    "argv": sys.argv[1:],
                    "LOCAL": os.environ.get("LOCAL"),
                    "BASE": os.environ.get("BASE"),
                    "REMOTE": os.environ.get("REMOTE"),
                    "MERGED": os.environ.get("MERGED"),
                    "local_content": content("LOCAL"),
                    "base_content": content("BASE"),
                    "remote_content": content("REMOTE"),
                }
            )
        )

    mode = os.environ.get("FAKE_MODE", "merged")
    content = os.environ.get("FAKE_CONTENT", "a\\nRESOLVED\\nc\\n")
    merged = os.environ.get("MERGED")
    local = os.environ.get("LOCAL")
    remote = os.environ.get("REMOTE")
    base = os.environ.get("BASE")

    if mode == "merged":
        Path(merged).write_text(content)
    elif mode == "local":
        Path(local).write_text(content)
    elif mode == "remote":
        Path(remote).write_text(content)
    elif mode == "nochange":
        pass
    elif mode == "markers":
        Path(merged).write_text(
            "<<<<<<< LOCAL\\na\\n=======\\nb\\n>>>>>>> REMOTE\\n"
        )
    elif mode == "empty":
        Path(merged).write_text("")
    elif mode == "base":
        Path(merged).write_bytes(Path(base).read_bytes())
    elif mode == "exit1":
        Path(merged).write_text(content)
        sys.exit(1)
    elif mode == "two-local":
        Path(local).write_text(content)
    elif mode == "two-remote":
        Path(remote).write_text(content)
    elif mode == "two-nochange":
        pass
    else:
        raise SystemExit(f"unknown FAKE_MODE: {mode}")
    """
)


class Sandbox:
    def __init__(self):
        self.base = Path(tempfile.mkdtemp(prefix="syncthing-merge-test-"))
        self.root = self.base / "sync"
        self.root.mkdir()
        self.backup = self.base / "backups"
        self.tmpdir = self.base / "tmp"
        self.tmpdir.mkdir()

    def cleanup(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)

    def write(self, relative: str, data: str | bytes) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data)
        return path

    def read(self, relative: str) -> str:
        return (self.root / relative).read_text()

    def exists(self, relative: str) -> bool:
        return (self.root / relative).exists()

    def conflict_path(self, original_name: str, stamp: str) -> Path:
        stem, ext = sm.split_name(original_name)
        name = f"{stem}.sync-conflict-{stamp}-ABCDEFG{ext}"
        return self.root / Path(original_name).parent / name

    def write_version(self, original_name: str, data: str, stamp: str) -> Path:
        stem, ext = sm.split_name(original_name)
        name = f"{stem}~{stamp}{ext}"
        path = self.root / ".stversions" / Path(original_name).parent / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        return path

    def setup_conflict(
        self,
        original_name: str = "f.txt",
        *,
        base: str = "a\nb\nc\n",
        local: str = "a\nLOCAL\nc\n",
        remote: str = "a\nREMOTE\nc\n",
        stamp: str = "20260101-120000",
        version_stamp: str = "20251231-120000",
        with_base: bool = True,
    ) -> Path:
        self.write(original_name, local)
        if with_base:
            self.write_version(original_name, base, version_stamp)
        conflict = self.conflict_path(original_name, stamp)
        conflict.parent.mkdir(parents=True, exist_ok=True)
        conflict.write_text(remote)
        return conflict

    def git(self, *args: str, env: dict[str, str] | None = None, check: bool = True):
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            check=check,
            env=full_env,
        )

    def git_init(self) -> None:
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")

    def commit(self, message: str = "base", date: str = "2025-12-31T12:00:00"):
        self.git("add", "-A")
        self.git(
            "commit",
            "-q",
            "-m",
            message,
            env={
                "GIT_AUTHOR_DATE": date,
                "GIT_COMMITTER_DATE": date,
            },
        )

    def configure_fake_tool(self) -> Path:
        if not (self.root / ".git").exists():
            self.git_init()
        script = self.base / "fake_tool.py"
        script.write_text(FAKE_TOOL)
        script.chmod(0o755)
        quoted = shlex.quote(str(script))
        self.git(
            "config",
            "mergetool.fake.cmd",
            f'{quoted} "$LOCAL" "$BASE" "$REMOTE" "$MERGED"',
        )
        self.git(
            "config",
            "difftool.fake.cmd",
            f'{quoted} "$LOCAL" "$REMOTE"',
        )
        return script

    def run(self, *args: str, env: dict[str, str] | None = None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        previous_tempdir = tempfile.tempdir
        tempfile.tempdir = str(self.tmpdir)
        merged_env = {"TMPDIR": str(self.tmpdir)}
        if env:
            merged_env.update(env)
        try:
            with (
                mock.patch.dict(os.environ, merged_env),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                code = sm.main([str(self.root), *args])
        finally:
            tempfile.tempdir = previous_tempdir
        return code, stdout.getvalue() + stderr.getvalue()

    def run_fake(
        self,
        *args: str,
        mode: str = "merged",
        content: str = "a\nRESOLVED\nc\n",
        backup: bool = True,
        env: dict[str, str] | None = None,
    ):
        record = self.base / "record.json"
        if record.exists():
            record.unlink()
        fake_env = {
            "FAKE_RECORD": str(record),
            "FAKE_MODE": mode,
            "FAKE_CONTENT": content,
        }
        if env:
            fake_env.update(env)
        argv = list(args)
        if backup and "--backup-dir" not in argv:
            argv.extend(["--backup-dir", str(self.backup)])
        code, output = self.run(*argv, env=fake_env)
        data = json.loads(record.read_text()) if record.exists() else None
        return code, output, data


class ParsingTests(unittest.TestCase):
    def test_conflict_names_round_trip(self) -> None:
        parsed = sm.parse_conflict_name(
            "archive.tar.sync-conflict-20260101-120000-ABCDEFG.gz"
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.original_name, "archive.tar.gz")
        self.assertEqual(parsed.stem, "archive.tar")
        self.assertEqual(parsed.ext, ".gz")
        self.assertEqual(parsed.device, "ABCDEFG")
        self.assertEqual(parsed.stamp.strftime(sm.STAMP_FORMAT), "20260101-120000")

        hidden = sm.parse_conflict_name(".sync-conflict-20260101-120000-ABCDEFG.env")
        self.assertIsNotNone(hidden)
        assert hidden is not None
        self.assertEqual(hidden.original_name, ".env")

        plain = sm.parse_conflict_name("notes.sync-conflict-20260101-120000-ABCDEFG")
        self.assertIsNotNone(plain)
        assert plain is not None
        self.assertEqual(plain.original_name, "notes")

    def test_invalid_conflict_name(self) -> None:
        self.assertIsNone(sm.parse_conflict_name("ordinary.txt"))
        self.assertIsNone(
            sm.parse_conflict_name(".sync-conflict-20260101-120000-ABCDEFG")
        )

    def test_trailing_dot_names(self) -> None:
        parsed = sm.parse_conflict_name("notes.sync-conflict-20260101-120000-ABCDEFG.")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.original_name, "notes.")
        self.assertIsNotNone(sm.parse_version_name("notes~20251231-120000.", "notes."))

    def test_version_names(self) -> None:
        self.assertIsNotNone(
            sm.parse_version_name("archive.tar~20251231-120000.gz", "archive.tar.gz")
        )
        self.assertIsNotNone(sm.parse_version_name("~20251231-120000.env", ".env"))
        self.assertIsNotNone(sm.parse_version_name("f.txt~20251231-120000", "f.txt"))
        self.assertIsNone(sm.parse_version_name("f.txt~20251231-120000.bak", "f.txt"))

    def test_split_name_matches_syncthing(self) -> None:
        self.assertEqual(sm.split_name("archive.tar.gz"), ("archive.tar", ".gz"))
        self.assertEqual(sm.split_name(".env"), ("", ".env"))
        self.assertEqual(sm.split_name("notes"), ("notes", ""))

    def test_tool_argument_order(self) -> None:
        self.assertEqual(
            sm.THREE_WAY_TOOLS["meld"],
            'meld --auto-merge --output="$MERGED" "$LOCAL" "$BASE" "$REMOTE"',
        )
        self.assertEqual(
            sm.THREE_WAY_TOOLS["vscode"],
            'code --wait --merge "$REMOTE" "$LOCAL" "$BASE" "$MERGED"',
        )
        self.assertIn(
            '-o "$MERGED" "$BASE" "$LOCAL" "$REMOTE"',
            sm.THREE_WAY_TOOLS["kdiff3"],
        )

    def test_builtin_command_adapts_alternate_executables(self) -> None:
        def fake_which(name: str) -> str | None:
            return {
                "bcompare": "/opt/bcompare",
                "vimdiff": "/usr/bin/vimdiff",
            }.get(name)

        with (
            mock.patch.object(sm, "git_config_get", return_value=""),
            mock.patch.object(sm.shutil, "which", side_effect=fake_which),
        ):
            beyond_compare = sm.tool_command("bc", Path("."))
            vimdiff = sm.tool_command("vimdiff", Path("."))

        assert beyond_compare is not None
        assert vimdiff is not None
        self.assertTrue(beyond_compare.startswith("/opt/bcompare "))
        self.assertIn('-mergeoutput="$MERGED"', beyond_compare)
        self.assertTrue(vimdiff.startswith("/usr/bin/vimdiff "))
        self.assertIn('"$MERGED"', vimdiff)


class MergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)

    def test_clean_auto_merge_installs_and_backs_up(self) -> None:
        conflict = self.sandbox.setup_conflict(
            base="a\nb\nc\n",
            local="a-LOCAL\nb\nc\n",
            remote="a\nb\nc-REMOTE\n",
        )
        code, output = self.sandbox.run(
            "--auto", "--backup-dir", str(self.sandbox.backup)
        )

        self.assertEqual(code, 0, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a-LOCAL\nb\nc-REMOTE\n")
        self.assertFalse(conflict.exists())

        runs = list(self.sandbox.backup.glob("*/*"))
        self.assertEqual(len(runs), 1, output)
        run_dir = runs[0]
        self.assertEqual((run_dir / "before" / "f.txt").read_text(), "a-LOCAL\nb\nc\n")
        self.assertEqual(
            (run_dir / "conflicts" / conflict.name).read_text(),
            "a\nb\nc-REMOTE\n",
        )
        self.assertEqual((run_dir / "base" / "f.txt").read_text(), "a\nb\nc\n")
        self.assertEqual(
            (run_dir / "merged" / "f.txt").read_text(),
            "a-LOCAL\nb\nc-REMOTE\n",
        )
        self.assertIn("automatic (3-way)", (run_dir / "MANIFEST.txt").read_text())

    def test_batch_auto_merge_processes_every_conflict(self) -> None:
        clean_a = self.sandbox.setup_conflict(
            "a.txt",
            base="1\n2\n3\n",
            local="A-LOCAL\n2\n3\n",
            remote="1\n2\nA-REMOTE\n",
        )
        clean_b = self.sandbox.setup_conflict(
            "b.txt",
            base="1\n2\n3\n",
            local="B-LOCAL\n2\n3\n",
            remote="1\n2\nB-REMOTE\n",
        )
        conflicting = self.sandbox.setup_conflict(
            "c.txt",
            base="1\n2\n3\n",
            local="1\nC-LOCAL\n3\n",
            remote="1\nC-REMOTE\n3\n",
        )

        code, output = self.sandbox.run(
            "--auto", "--backup-dir", str(self.sandbox.backup)
        )

        self.assertEqual(code, 2, output)
        self.assertEqual(self.sandbox.read("a.txt"), "A-LOCAL\n2\nA-REMOTE\n")
        self.assertEqual(self.sandbox.read("b.txt"), "B-LOCAL\n2\nB-REMOTE\n")
        self.assertEqual(self.sandbox.read("c.txt"), "1\nC-LOCAL\n3\n")
        self.assertFalse(clean_a.exists())
        self.assertFalse(clean_b.exists())
        self.assertTrue(conflicting.exists())
        self.assertIn("resolved:   2", output)
        self.assertIn("unresolved: 1", output)

    def test_original_changing_during_backup_is_not_overwritten(self) -> None:
        conflict = self.sandbox.setup_conflict(
            base="a\nb\nc\n",
            local="a-LOCAL\nb\nc\n",
            remote="a\nb\nc-REMOTE\n",
        )
        original = self.sandbox.root / "f.txt"
        real_backup_install = sm.App.backup_install

        def backup_then_change(app, item, base, merged, **kwargs):
            result = real_backup_install(app, item, base, merged, **kwargs)
            original.write_text("EXTERNAL\n")
            return result

        with mock.patch.object(sm.App, "backup_install", backup_then_change):
            code, output = self.sandbox.run(
                "--auto", "--backup-dir", str(self.sandbox.backup)
            )

        self.assertEqual(code, 2, output)
        self.assertEqual(original.read_text(), "EXTERNAL\n")
        self.assertTrue(conflict.exists())
        self.assertIn("original file changed while merging", output)

    def test_conflict_changing_during_backup_is_not_removed(self) -> None:
        conflict = self.sandbox.setup_conflict(
            base="a\nb\nc\n",
            local="a-LOCAL\nb\nc\n",
            remote="a\nb\nc-REMOTE\n",
        )
        real_backup_install = sm.App.backup_install

        def backup_then_change(app, item, base, merged, **kwargs):
            result = real_backup_install(app, item, base, merged, **kwargs)
            item.path.write_text("a\nCHANGED\nc\n")
            return result

        with mock.patch.object(sm.App, "backup_install", backup_then_change):
            code, output = self.sandbox.run(
                "--auto", "--backup-dir", str(self.sandbox.backup)
            )

        self.assertEqual(code, 2, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a-LOCAL\nb\nc\n")
        self.assertEqual(conflict.read_text(), "a\nCHANGED\nc\n")
        self.assertIn("conflict file changed while merging", output)

    def test_restore_missing_refuses_if_original_reappears(self) -> None:
        conflict = self.sandbox.setup_conflict()
        original = self.sandbox.root / "f.txt"
        original.unlink()
        real_backup_install = sm.App.backup_install

        def backup_then_recreate(app, item, base, merged, **kwargs):
            result = real_backup_install(app, item, base, merged, **kwargs)
            original.write_text("NEW\n")
            return result

        with mock.patch.object(sm.App, "backup_install", backup_then_recreate):
            code, output = self.sandbox.run(
                "--restore-missing",
                "--yes",
                "--backup-dir",
                str(self.sandbox.backup),
            )

        self.assertEqual(code, 2, output)
        self.assertEqual(original.read_text(), "NEW\n")
        self.assertTrue(conflict.exists())
        self.assertIn("original file appeared while merging", output)

    def test_conflicting_auto_merge_leaves_live_file_untouched(self) -> None:
        conflict = self.sandbox.setup_conflict()
        code, output = self.sandbox.run(
            "--auto", "--backup-dir", str(self.sandbox.backup)
        )

        self.assertEqual(code, 2, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a\nLOCAL\nc\n")
        self.assertTrue(conflict.exists())
        for marker in ("<<<<<<<", "=======", ">>>>>>>"):
            self.assertNotIn(marker, self.sandbox.read("f.txt"))
        self.assertIn("visual tools are disabled", output)

    def test_missing_tool_keeps_conflict(self) -> None:
        conflict = self.sandbox.setup_conflict()
        code, output = self.sandbox.run(
            "--tool",
            "does-not-exist",
            "--backup-dir",
            str(self.sandbox.backup),
        )

        self.assertEqual(code, 2, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a\nLOCAL\nc\n")
        self.assertTrue(conflict.exists())
        self.assertIn("conflict preserved", output)

    def test_visual_tool_writes_merged_pane(self) -> None:
        conflict = self.sandbox.setup_conflict()
        self.sandbox.configure_fake_tool()
        code, output, record = self.sandbox.run_fake(
            "--tool",
            "fake",
            mode="merged",
            content="a\nRESOLVED\nc\n",
        )

        self.assertEqual(code, 0, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a\nRESOLVED\nc\n")
        self.assertFalse(conflict.exists())
        assert record is not None
        self.assertEqual(record["local_content"], "a\nLOCAL\nc\n")
        self.assertEqual(record["base_content"], "a\nb\nc\n")
        self.assertEqual(record["remote_content"], "a\nREMOTE\nc\n")
        self.assertEqual(
            record["argv"],
            [
                record["LOCAL"],
                record["BASE"],
                record["REMOTE"],
                record["MERGED"],
            ],
        )
        self.assertNotEqual(record["LOCAL"], str(self.sandbox.root / "f.txt"))
        self.assertNotEqual(record["MERGED"], str(self.sandbox.root / "f.txt"))

    def test_tool_editing_local_pane_is_refused(self) -> None:
        conflict = self.sandbox.setup_conflict()
        self.sandbox.configure_fake_tool()
        code, output, _ = self.sandbox.run_fake(
            "--tool",
            "fake",
            mode="local",
            content="a\nWRONG-PANE\nc\n",
        )

        self.assertEqual(code, 2, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a\nLOCAL\nc\n")
        self.assertTrue(conflict.exists())
        self.assertIn("modified LOCAL", output)

    def test_tool_leaving_markers_is_refused(self) -> None:
        conflict = self.sandbox.setup_conflict()
        self.sandbox.configure_fake_tool()
        code, output, _ = self.sandbox.run_fake("--tool", "fake", mode="markers")

        self.assertEqual(code, 2, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a\nLOCAL\nc\n")
        self.assertTrue(conflict.exists())
        self.assertIn("conflict markers", output)

    def test_tool_unchanged_result_is_refused(self) -> None:
        conflict = self.sandbox.setup_conflict()
        self.sandbox.configure_fake_tool()
        code, output, _ = self.sandbox.run_fake("--tool", "fake", mode="nochange")

        self.assertEqual(code, 2, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a\nLOCAL\nc\n")
        self.assertTrue(conflict.exists())
        self.assertIn("did not change", output)

    def test_tool_nonzero_exit_keeps_conflict(self) -> None:
        conflict = self.sandbox.setup_conflict()
        self.sandbox.configure_fake_tool()
        code, output, _ = self.sandbox.run_fake("--tool", "fake", mode="exit1")

        self.assertEqual(code, 2, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a\nLOCAL\nc\n")
        self.assertTrue(conflict.exists())
        self.assertIn("exited with code 1", output)

    def test_tool_empty_result_is_refused(self) -> None:
        conflict = self.sandbox.setup_conflict()
        self.sandbox.configure_fake_tool()
        code, output, _ = self.sandbox.run_fake("--tool", "fake", mode="empty")

        self.assertEqual(code, 2, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a\nLOCAL\nc\n")
        self.assertTrue(conflict.exists())
        self.assertIn("result is empty", output)

    def test_tool_result_equal_to_base_is_refused(self) -> None:
        conflict = self.sandbox.setup_conflict()
        self.sandbox.configure_fake_tool()
        code, output, _ = self.sandbox.run_fake("--tool", "fake", mode="base")

        self.assertEqual(code, 2, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a\nLOCAL\nc\n")
        self.assertTrue(conflict.exists())
        self.assertIn("identical to the base", output)

    def test_two_way_edit_local_installs(self) -> None:
        conflict = self.sandbox.setup_conflict()
        self.sandbox.configure_fake_tool()
        code, output, record = self.sandbox.run_fake(
            "--tool",
            "fake",
            "--two-way",
            mode="two-local",
            content="a\nTWO-WAY\nc\n",
        )

        self.assertEqual(code, 0, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a\nTWO-WAY\nc\n")
        self.assertFalse(conflict.exists())
        assert record is not None
        self.assertEqual(record["base_content"], "")

    def test_two_way_edit_remote_is_refused(self) -> None:
        conflict = self.sandbox.setup_conflict()
        self.sandbox.configure_fake_tool()
        code, output, _ = self.sandbox.run_fake(
            "--tool",
            "fake",
            "--two-way",
            mode="two-remote",
            content="a\nWRONG-PANE\nc\n",
        )

        self.assertEqual(code, 2, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a\nLOCAL\nc\n")
        self.assertTrue(conflict.exists())
        self.assertIn("modified REMOTE", output)

    def test_two_way_no_change_is_refused(self) -> None:
        conflict = self.sandbox.setup_conflict()
        self.sandbox.configure_fake_tool()
        code, output, _ = self.sandbox.run_fake(
            "--tool", "fake", "--two-way", mode="two-nochange"
        )

        self.assertEqual(code, 2, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a\nLOCAL\nc\n")
        self.assertTrue(conflict.exists())
        self.assertIn("LOCAL was not modified", output)

    def test_git_base_used_for_clean_merge(self) -> None:
        self.sandbox.git_init()
        self.sandbox.write("f.txt", "a\nb\nc\n")
        self.sandbox.commit()
        self.sandbox.write("f.txt", "a-LOCAL\nb\nc\n")
        conflict = self.sandbox.conflict_path("f.txt", "20260101-120000")
        conflict.write_text("a\nb\nc-REMOTE\n")

        code, output = self.sandbox.run(
            "--auto", "--backup-dir", str(self.sandbox.backup)
        )

        self.assertEqual(code, 0, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a-LOCAL\nb\nc-REMOTE\n")
        self.assertFalse(conflict.exists())
        runs = list(self.sandbox.backup.glob("*/*"))
        self.assertEqual(len(runs), 1, output)
        self.assertEqual((runs[0] / "base" / "f.txt").read_text(), "a\nb\nc\n")
        self.assertIn("Git ", (runs[0] / "MANIFEST.txt").read_text())

    def test_git_base_is_preferred_over_stversions(self) -> None:
        self.sandbox.git_init()
        self.sandbox.write("f.txt", "a\nb\nc\n")
        self.sandbox.commit()
        self.sandbox.write_version("f.txt", "WRONG\nBASE\n", "20251231-120000")
        self.sandbox.write("f.txt", "a-LOCAL\nb\nc\n")
        conflict = self.sandbox.conflict_path("f.txt", "20260101-120000")
        conflict.write_text("a\nb\nc-REMOTE\n")

        code, output = self.sandbox.run(
            "--auto", "--backup-dir", str(self.sandbox.backup)
        )

        self.assertEqual(code, 0, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a-LOCAL\nb\nc-REMOTE\n")
        runs = list(self.sandbox.backup.glob("*/*"))
        self.assertEqual(len(runs), 1, output)
        self.assertEqual((runs[0] / "base" / "f.txt").read_text(), "a\nb\nc\n")

    def test_stversions_future_version_is_ignored(self) -> None:
        self.sandbox.write("f.txt", "a-LOCAL\nb\nc\n")
        self.sandbox.write_version("f.txt", "a\nb\nc\n", "20251231-120000")
        self.sandbox.write_version("f.txt", "X\nY\nZ\n", "20260201-120000")
        conflict = self.sandbox.conflict_path("f.txt", "20260101-120000")
        conflict.write_text("a\nb\nc-REMOTE\n")

        code, output = self.sandbox.run(
            "--auto", "--verbose", "--backup-dir", str(self.sandbox.backup)
        )

        self.assertEqual(code, 0, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a-LOCAL\nb\nc-REMOTE\n")
        self.assertIn("ignoring future version", output)

    def test_require_clean_aborts_before_touching_files(self) -> None:
        self.sandbox.git_init()
        self.sandbox.write("f.txt", "a\nb\nc\n")
        self.sandbox.commit()
        self.sandbox.write("f.txt", "a\nLOCAL\nc\n")
        conflict = self.sandbox.conflict_path("f.txt", "20260101-120000")
        conflict.write_text("a\nREMOTE\nc\n")

        code, output = self.sandbox.run(
            "--auto",
            "--require-clean",
            "--backup-dir",
            str(self.sandbox.backup),
        )

        self.assertEqual(code, 1, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a\nLOCAL\nc\n")
        self.assertTrue(conflict.exists())
        self.assertIn("uncommitted tracked changes", output)
        self.assertFalse(self.sandbox.backup.exists())

    def test_restore_missing_original(self) -> None:
        conflict = self.sandbox.conflict_path("f.txt", "20260101-120000")
        conflict.write_text("restored\n")

        code, output = self.sandbox.run(
            "--restore-missing",
            "--yes",
            "--auto",
            "--backup-dir",
            str(self.sandbox.backup),
        )

        self.assertEqual(code, 0, output)
        self.assertEqual(self.sandbox.read("f.txt"), "restored\n")
        self.assertFalse(conflict.exists())
        runs = list(self.sandbox.backup.glob("*/*"))
        self.assertEqual(len(runs), 1, output)
        self.assertEqual((runs[0] / "restored" / "f.txt").read_text(), "restored\n")
        self.assertEqual(
            (runs[0] / "conflicts" / conflict.name).read_text(),
            "restored\n",
        )

    def test_missing_original_is_skipped_without_flag(self) -> None:
        conflict = self.sandbox.conflict_path("f.txt", "20260101-120000")
        conflict.write_text("remote\n")

        code, output = self.sandbox.run(
            "--auto", "--backup-dir", str(self.sandbox.backup)
        )

        self.assertEqual(code, 2, output)
        self.assertFalse(self.sandbox.exists("f.txt"))
        self.assertTrue(conflict.exists())
        self.assertIn("--restore-missing", output)

    def test_restore_missing_refuses_markers(self) -> None:
        conflict = self.sandbox.conflict_path("f.txt", "20260101-120000")
        conflict.write_text("<<<<<<< LOCAL\na\n=======\nb\n>>>>>>> REMOTE\n")

        code, output = self.sandbox.run(
            "--restore-missing",
            "--yes",
            "--auto",
            "--backup-dir",
            str(self.sandbox.backup),
        )

        self.assertEqual(code, 2, output)
        self.assertFalse(self.sandbox.exists("f.txt"))
        self.assertTrue(conflict.exists())
        self.assertIn("conflict markers", output)

    def test_binary_conflict_is_left_alone(self) -> None:
        original = self.sandbox.write("f.bin", b"\x00local")
        conflict = self.sandbox.conflict_path("f.bin", "20260101-120000")
        conflict.write_bytes(b"\x00remote")

        code, output = self.sandbox.run(
            "--auto", "--backup-dir", str(self.sandbox.backup)
        )

        self.assertEqual(code, 2, output)
        self.assertEqual(original.read_bytes(), b"\x00local")
        self.assertEqual(conflict.read_bytes(), b"\x00remote")
        self.assertIn("binary", output)

    def test_symlink_conflict_is_skipped(self) -> None:
        self.sandbox.write("f.txt", "local\n")
        target = self.sandbox.write("target.txt", "remote\n")
        conflict = self.sandbox.conflict_path("f.txt", "20260101-120000")
        try:
            conflict.symlink_to(target)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlinks are unavailable: {error}")

        code, output = self.sandbox.run(
            "--auto", "--backup-dir", str(self.sandbox.backup)
        )

        self.assertEqual(code, 2, output)
        self.assertEqual(self.sandbox.read("f.txt"), "local\n")
        self.assertTrue(conflict.is_symlink())
        self.assertIn("symlink", output)

    def test_dry_run_changes_nothing(self) -> None:
        conflict = self.sandbox.setup_conflict(
            base="a\nb\nc\n",
            local="a-LOCAL\nb\nc\n",
            remote="a\nb\nc-REMOTE\n",
        )

        code, output = self.sandbox.run(
            "--dry-run", "--auto", "--backup-dir", str(self.sandbox.backup)
        )

        self.assertEqual(code, 0, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a-LOCAL\nb\nc\n")
        self.assertTrue(conflict.exists())
        self.assertFalse(self.sandbox.backup.exists())
        self.assertIn("would install", output)
        self.assertIn("Dry run complete", output)

    def test_list_conflicts(self) -> None:
        conflict = self.sandbox.setup_conflict()

        code, output = self.sandbox.run("--list")

        self.assertEqual(code, 0, output)
        self.assertIn(conflict.name, output)
        self.assertIn("stamp=2026-01-01 12:00:00", output)
        self.assertIn("original=f.txt", output)
        self.assertFalse(self.sandbox.backup.exists())

    def test_multi_dot_and_hidden_filenames(self) -> None:
        archive_conflict = self.sandbox.setup_conflict(
            "archive.tar.gz",
            base="a\nb\nc\n",
            local="a-LOCAL\nb\nc\n",
            remote="a\nb\nc-REMOTE\n",
        )
        env_conflict = self.sandbox.setup_conflict(
            ".env",
            base="x\ny\nz\n",
            local="x-LOCAL\ny\nz\n",
            remote="x\ny\nz-REMOTE\n",
        )

        code, output = self.sandbox.run(
            "--auto", "--backup-dir", str(self.sandbox.backup)
        )

        self.assertEqual(code, 0, output)
        self.assertEqual(self.sandbox.read("archive.tar.gz"), "a-LOCAL\nb\nc-REMOTE\n")
        self.assertEqual(self.sandbox.read(".env"), "x-LOCAL\ny\nz-REMOTE\n")
        self.assertFalse(archive_conflict.exists())
        self.assertFalse(env_conflict.exists())
        runs = list(self.sandbox.backup.glob("*/*"))
        self.assertEqual(len(runs), 1, output)
        run_dir = runs[0]
        self.assertEqual(
            (run_dir / "merged" / "archive.tar.gz").read_text(),
            "a-LOCAL\nb\nc-REMOTE\n",
        )
        self.assertEqual(
            (run_dir / "merged" / ".env").read_text(),
            "x-LOCAL\ny\nz-REMOTE\n",
        )

    def test_keep_conflicts(self) -> None:
        conflict = self.sandbox.setup_conflict(
            base="a\nb\nc\n",
            local="a-LOCAL\nb\nc\n",
            remote="a\nb\nc-REMOTE\n",
        )

        code, output = self.sandbox.run(
            "--auto",
            "--keep-conflicts",
            "--backup-dir",
            str(self.sandbox.backup),
        )

        self.assertEqual(code, 0, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a-LOCAL\nb\nc-REMOTE\n")
        self.assertTrue(conflict.exists())
        self.assertIn("kept conflict file", output)

    def test_allow_input_markers(self) -> None:
        self.sandbox.write("f.txt", "header-local\n=======\nfooter\n")
        self.sandbox.write_version(
            "f.txt", "header\n=======\nfooter\n", "20251231-120000"
        )
        conflict = self.sandbox.conflict_path("f.txt", "20260101-120000")
        conflict.write_text("header\n=======\nfooter-remote\n")

        code, output = self.sandbox.run(
            "--auto", "--backup-dir", str(self.sandbox.backup)
        )
        self.assertEqual(code, 2, output)
        self.assertEqual(self.sandbox.read("f.txt"), "header-local\n=======\nfooter\n")
        self.assertTrue(conflict.exists())
        self.assertIn("conflict markers", output)

        code, output = self.sandbox.run(
            "--auto",
            "--allow-input-markers",
            "--backup-dir",
            str(self.sandbox.backup),
        )
        self.assertEqual(code, 0, output)
        self.assertEqual(
            self.sandbox.read("f.txt"),
            "header-local\n=======\nfooter-remote\n",
        )
        self.assertFalse(conflict.exists())

    def test_no_auto_merge_opens_tool(self) -> None:
        conflict = self.sandbox.setup_conflict()
        self.sandbox.configure_fake_tool()
        code, output, record = self.sandbox.run_fake(
            "--tool", "fake", "--no-auto-merge"
        )

        self.assertEqual(code, 0, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a\nRESOLVED\nc\n")
        self.assertFalse(conflict.exists())
        self.assertIsNotNone(record)

    def test_empty_base_three_way(self) -> None:
        self.sandbox.write("f.txt", "a\nLOCAL\nc\n")
        conflict = self.sandbox.conflict_path("f.txt", "20260101-120000")
        conflict.write_text("a\nREMOTE\nc\n")
        self.sandbox.configure_fake_tool()

        code, output, record = self.sandbox.run_fake("--tool", "fake", "--empty-base")

        self.assertEqual(code, 0, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a\nRESOLVED\nc\n")
        assert record is not None
        self.assertEqual(record["base_content"], "")
        self.assertEqual(record["local_content"], "a\nLOCAL\nc\n")
        self.assertEqual(record["remote_content"], "a\nREMOTE\nc\n")

    def test_backup_dir_inside_root_is_skipped(self) -> None:
        self.sandbox.setup_conflict(
            base="a\nb\nc\n",
            local="a-LOCAL\nb\nc\n",
            remote="a\nb\nc-REMOTE\n",
        )
        inside_backup = self.sandbox.root / "backups"

        code, output = self.sandbox.run("--auto", "--backup-dir", str(inside_backup))
        self.assertEqual(code, 0, output)
        backup_conflicts = list(inside_backup.glob("*/*/conflicts/*.sync-conflict-*"))
        self.assertTrue(backup_conflicts)

        code, output = self.sandbox.run("--list", "--backup-dir", str(inside_backup))
        self.assertEqual(code, 0, output)
        self.assertIn("No conflict files found", output)
        self.assertNotIn("backups/", output)

    def test_backup_parent_that_is_an_ancestor_does_not_hide_root(self) -> None:
        conflict = self.sandbox.setup_conflict()

        code, output = self.sandbox.run(
            "--list", "--backup-dir", str(self.sandbox.base)
        )

        self.assertEqual(code, 0, output)
        self.assertIn(conflict.name, output)

    def test_backup_dir_equal_to_root_is_rejected(self) -> None:
        self.sandbox.setup_conflict()

        code, output = self.sandbox.run(
            "--list", "--backup-dir", str(self.sandbox.root)
        )

        self.assertEqual(code, 1, output)
        self.assertIn("cannot be the sync root", output)

    def test_no_backup_requires_yes_in_non_tty(self) -> None:
        conflict = self.sandbox.setup_conflict(
            base="a\nb\nc\n",
            local="a-LOCAL\nb\nc\n",
            remote="a\nb\nc-REMOTE\n",
        )

        with mock.patch.object(sys, "stdin", io.StringIO("")):
            code, output = self.sandbox.run("--auto", "--no-backup")
        self.assertEqual(code, 2, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a-LOCAL\nb\nc\n")
        self.assertTrue(conflict.exists())
        self.assertIn("requires --yes", output)

        with mock.patch.object(sys, "stdin", io.StringIO("")):
            code, output = self.sandbox.run("--auto", "--no-backup", "--yes")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.sandbox.read("f.txt"), "a-LOCAL\nb\nc-REMOTE\n")
        self.assertFalse(conflict.exists())
        self.assertFalse(self.sandbox.backup.exists())

    def test_doctor_with_custom_tool(self) -> None:
        self.sandbox.setup_conflict()
        self.sandbox.configure_fake_tool()

        code, output = self.sandbox.run("--doctor", "--tool", "fake")

        self.assertEqual(code, 0, output)
        self.assertIn("Conflicts: 1", output)
        self.assertIn("No problems detected", output)
        self.assertIn("custom Git command", output)

    def test_doctor_reports_missing_tool(self) -> None:
        code, output = self.sandbox.run("--doctor", "--tool", "does-not-exist")

        self.assertEqual(code, 1, output)
        self.assertIn("NOT FOUND", output)
        self.assertIn("no 3-way command", output)


if __name__ == "__main__":
    unittest.main()
