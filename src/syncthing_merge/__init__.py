#!/usr/bin/env python3
import os
import re
import subprocess
import tempfile
from pathlib import Path
from shutil import which

# --- CONFIGURATION ---
# Options: "auto" (reads your Git config), or force a specific tool name (e.g., "kdiff3")
MERGE_TOOL = "auto"

# Match Syncthing conflict files: e.g., "file.sync-conflict-20260606-120000-ABC.txt"
CONFLICT_REGEX = re.compile(r"^(.*)\.sync-conflict-\d{8}-\d{6}-\w+(\..+)?$")

# Built-in fallback command templates for popular tools
BUILTIN_3WAY_TOOLS = {
    "meld": 'meld "$BASE" "$LOCAL" "$REMOTE" --output "$MERGED"',
    "vscode": 'code --wait --merge "$REMOTE" "$LOCAL" "$BASE" "$MERGED"',
    "code": 'code --wait --merge "$REMOTE" "$LOCAL" "$BASE" "$MERGED"',
    "kdiff3": 'kdiff3 "$BASE" "$LOCAL" "$REMOTE" -o "$MERGED"',
    "p4merge": 'p4merge "$BASE" "$LOCAL" "$REMOTE" "$MERGED"',
    "bc": 'bcomp "$LOCAL" "$REMOTE" "$BASE" "$MERGED"',
    "bc3": 'bcomp "$LOCAL" "$REMOTE" "$BASE" "$MERGED"',
    "bc4": 'bcomp "$LOCAL" "$REMOTE" "$BASE" "$MERGED"',
    "vimdiff": 'vimdiff -f -d -c "wincmd J" "$MERGED" "$LOCAL" "$BASE" "$REMOTE"',
    "opendiff": 'opendiff "$LOCAL" "$REMOTE" -ancestor "$BASE" -merge "$MERGED"',
}

BUILTIN_2WAY_TOOLS = {
    "meld": 'meld "$LOCAL" "$REMOTE"',
    "vscode": 'code --wait --diff "$LOCAL" "$REMOTE"',
    "code": 'code --wait --diff "$LOCAL" "$REMOTE"',
    "kdiff3": 'kdiff3 "$LOCAL" "$REMOTE"',
    "p4merge": 'p4merge "$LOCAL" "$REMOTE"',
    "bc": 'bcomp "$LOCAL" "$REMOTE"',
    "bc3": 'bcomp "$LOCAL" "$REMOTE"',
    "bc4": 'bcomp "$LOCAL" "$REMOTE"',
    "vimdiff": 'vimdiff -d "$LOCAL" "$REMOTE"',
    "opendiff": 'opendiff "$LOCAL" "$REMOTE"',
}


def get_git_config(key):
    """Safely queries a git configuration key."""
    try:
        return subprocess.run(
            ["git", "config", "--get", key], capture_output=True, text=True
        ).stdout.strip()
    except FileNotFoundError:
        return ""


def detect_preferred_tool():
    """
    Finds the active merge tool in Git config, or falls back to system scanning.
    """
    if MERGE_TOOL != "auto":
        return MERGE_TOOL.lower()

    # 1. Read standard Git configuration
    git_tool = get_git_config("merge.tool")
    if git_tool:
        return git_tool.lower()

    # 2. Fallback to scanning the system PATH for known tools
    for tool in ["meld", "code", "kdiff3", "bcomp", "p4merge"]:
        if which(tool):
            return "vscode" if tool == "code" else tool

    return "meld"


def get_merge_command_template(tool_name, is_3way=True):
    """
    Returns the command template for the given tool, prioritizing
    custom commands defined in the user's Git configuration.
    """
    config_key = (
        f"mergetool.{tool_name}.cmd" if is_3way else f"difftool.{tool_name}.cmd"
    )
    custom_cmd = get_git_config(config_key)
    if custom_cmd:
        return custom_cmd

    # Fallback to built-in command templates
    presets = BUILTIN_3WAY_TOOLS if is_3way else BUILTIN_2WAY_TOOLS
    return presets.get(tool_name)


def run_visual_tool(cmd_template, base, current, conflict):
    """
    Replaces placeholders in the command template and executes the merge tool.
    """
    # Normalize paths to use forward slashes for cross-platform compatibility
    base_str = str(base).replace("\\", "/")
    current_str = str(current).replace("\\", "/")
    conflict_str = str(conflict).replace("\\", "/")

    cmd_str = cmd_template
    replacements = [
        ("$BASE", base_str),
        ("%BASE%", base_str),
        ("$LOCAL", current_str),
        ("%LOCAL%", current_str),
        ("$REMOTE", conflict_str),
        ("%REMOTE%", conflict_str),
        ("$MERGED", current_str),
        ("%MERGED%", current_str),
    ]

    # Replace both quoted and unquoted placeholders safely
    for var, val in replacements:
        cmd_str = cmd_str.replace(f'"{var}"', f'"{val}"')
        cmd_str = cmd_str.replace(f"'{var}'", f"'{val}'")
        cmd_str = cmd_str.replace(var, f'"{val}"')

    print(f"    -> Executing: {cmd_str}")
    try:
        # Run with shell=True so the system shell tokenizes the command string
        subprocess.run(cmd_str, shell=True, check=True)
        return True
    except subprocess.CalledProcessError as e:
        # Tools may exit with non-zero on close or abort, which is handled gracefully
        print(f"    [INFO] Visual tool process finished (exit code {e.returncode}).")
        return True
    except FileNotFoundError:
        print(
            "    [ERROR] Failed to execute visual tool. Verify it is in your system PATH."
        )
        return False


def find_original_and_extension(conflict_file_name):
    """Parses a conflict filename to reconstruct the original filename."""
    match = CONFLICT_REGEX.match(conflict_file_name)
    if not match:
        return None, None
    base_name = match.group(1)
    ext = match.group(2) if match.group(2) else ""
    return f"{base_name}{ext}", ext


def find_base_ancestor(sync_root, relative_dir, original_name, ext):
    """Attempts to find the closest common ancestor inside the .stversions folder."""
    stversions_dir = sync_root / ".stversions" / relative_dir
    if not stversions_dir.is_dir():
        return None

    name_without_ext = (
        original_name[: -len(ext)]
        if ext and original_name.endswith(ext)
        else original_name
    )
    pattern = f"{name_without_ext}~*"
    if ext:
        pattern += ext

    versions = list(stversions_dir.glob(pattern))
    if not versions:
        return None

    versions.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return versions[0]


def run_git_empty_fallback(current_file, conflict_path):
    """Fallback: runs git merge-file with an empty temp file as the base."""
    print("    - Falling back to git merge-file with an empty base...")
    temp_base = tempfile.NamedTemporaryFile(delete=False)
    temp_base.close()
    base_file = Path(temp_base.name)

    try:
        result = subprocess.run(
            [
                "git",
                "merge-file",
                "-q",
                str(current_file),
                str(base_file),
                str(conflict_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"    [SUCCESS] Cleanly merged! Deleting conflict file.")
            conflict_path.unlink()
        elif result.returncode > 0:
            print(
                f"    [CONFLICTS] Merged with conflict markers. Resolve manually inside '{current_file.name}'."
            )
        else:
            print(f"    [ERROR] Git merge-file failed: {result.stderr.strip()}")
    except FileNotFoundError:
        print("    [ERROR] Git is not installed or not in your system PATH.")
    finally:
        if os.path.exists(base_file):
            os.unlink(base_file)


def merge_conflict(sync_root, conflict_path, tool):
    relative_dir = conflict_path.parent.relative_to(sync_root)
    original_name, ext = find_original_and_extension(conflict_path.name)

    if not original_name:
        return

    current_file = conflict_path.parent / original_name
    if not current_file.exists():
        print(f"[-] Original file does not exist for: {conflict_path.name}. Skipping.")
        return

    print(f"\n[+] Processing: {original_name}")

    # 1. Look for a common ancestor in .stversions
    base_file = find_base_ancestor(sync_root, relative_dir, original_name, ext)

    if base_file:
        print(f"    - Found common ancestor in versioning: {base_file.name}")
        try:
            # Try automatic silent git merge first
            result = subprocess.run(
                [
                    "git",
                    "merge-file",
                    "-q",
                    str(current_file),
                    str(base_file),
                    str(conflict_path),
                ],
                capture_output=True,
                text=True,
            )

            if result.returncode == 0:
                print(f"    [SUCCESS] Cleanly merged! Deleting conflict file.")
                conflict_path.unlink()
            elif result.returncode > 0:
                print(
                    f"    [CONFLICTS] Automatic merge failed. Overlapping changes found."
                )
                ans = (
                    input(
                        f"    Would you like to open {tool.upper()} to resolve conflicts? (y/N): "
                    )
                    .strip()
                    .lower()
                )
                if ans == "y":
                    cmd_template = get_merge_command_template(tool, is_3way=True)
                    if cmd_template:
                        success = run_visual_tool(
                            cmd_template, base_file, current_file, conflict_path
                        )
                        if success:
                            ans_del = (
                                input(
                                    f"    Did you successfully resolve the conflict? Delete '{conflict_path.name}'? (y/N): "
                                )
                                .strip()
                                .lower()
                            )
                            if ans_del == "y":
                                conflict_path.unlink()
                                print("    [SUCCESS] Deleted conflict file.")
                    else:
                        print(
                            f"    [ERROR] No visual merge template command resolved for '{tool}'."
                        )
            else:
                print(f"    [ERROR] Git merge-file failed: {result.stderr.strip()}")
        except FileNotFoundError:
            print("    [ERROR] Git is not installed or not in your system PATH.")
    else:
        # 2. NO common ancestor -> Launch preferred tool for a 2-way comparison
        print("    - No common ancestor found in .stversions.")
        cmd_template = get_merge_command_template(tool, is_3way=False)
        tool_launched = False
        if cmd_template:
            tool_launched = run_visual_tool(
                cmd_template, current_file, current_file, conflict_path
            )
            if tool_launched:
                ans_del = (
                    input(
                        f"    Did you successfully resolve the conflict? Delete '{conflict_path.name}'? (y/N): "
                    )
                    .strip()
                    .lower()
                )
                if ans_del == "y":
                    conflict_path.unlink()
                    print("    [SUCCESS] Deleted conflict file.")

        # Fallback to standard empty-base git merge if visual launch failed
        if not tool_launched:
            run_git_empty_fallback(current_file, conflict_path)


def main():
    sync_root = Path(os.getcwd())
    exclude_dirs = {".stversions", ".git"}

    # Detect the preferred merge tool
    tool = detect_preferred_tool()

    print(f"Scanning for Syncthing conflicts in: {sync_root}")
    print(f"Preferred visual merge tool: {tool.upper()}")

    conflict_files = []

    for root, dirs, files in os.walk(sync_root):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        for file in files:
            if CONFLICT_REGEX.match(file):
                conflict_files.append(Path(root) / file)

    if not conflict_files:
        print("No conflict files found.")
        return

    print(f"Found {len(conflict_files)} conflict files.")
    for path in conflict_files:
        merge_conflict(sync_root, path, tool)


if __name__ == "__main__":
    main()
