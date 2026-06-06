#!/usr/bin/env python3
import os
import re
import subprocess
import tempfile
from pathlib import Path

# Match Syncthing conflict files: e.g., "file.sync-conflict-20260606-120000-ABC.txt"
CONFLICT_REGEX = re.compile(r"^(.*)\.sync-conflict-\d{8}-\d{6}-\w+(\..+)?$")


def find_original_and_extension(conflict_file_name):
    """
    Parses a conflict filename to reconstruct the original filename and extension.
    """
    match = CONFLICT_REGEX.match(conflict_file_name)
    if not match:
        return None, None
    base_name = match.group(1)
    ext = match.group(2) if match.group(2) else ""
    return f"{base_name}{ext}", ext


def find_base_ancestor(sync_root, relative_dir, original_name, ext):
    """
    Attempts to find the closest common ancestor inside the .stversions folder.
    """
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


def launch_meld_2way(current, conflict):
    """
    Launches Meld for a 2-way merge (Current vs Conflict).
    """
    try:
        print(f"    -> Opening Meld (2-way merge)...")
        subprocess.run(["meld", str(current), str(conflict)])

        # After Meld closes, ask the user if they want to clean up the conflict file
        ans = (
            input(
                f"    Did you successfully resolve the conflict? Delete '{conflict.name}'? (y/N): "
            )
            .strip()
            .lower()
        )
        if ans == "y":
            conflict.unlink()
            print("    [SUCCESS] Deleted conflict file.")
        else:
            print("    [INFO] Kept conflict file.")
        return True
    except FileNotFoundError:
        print("    [WARNING] 'meld' command was not found in your system PATH.")
        return False


def launch_meld_3way(current, base, conflict):
    """
    Launches Meld for a 3-way merge (Current vs Base vs Conflict).
    """
    try:
        print(f"    -> Opening Meld (3-way merge)...")
        subprocess.run(["meld", str(current), str(base), str(conflict)])

        ans = (
            input(
                f"    Did you successfully resolve the conflict? Delete '{conflict.name}'? (y/N): "
            )
            .strip()
            .lower()
        )
        if ans == "y":
            conflict.unlink()
            print("    [SUCCESS] Deleted conflict file.")
        else:
            print("    [INFO] Kept conflict file.")
        return True
    except FileNotFoundError:
        print("    [WARNING] 'meld' command was not found in your system PATH.")
        return False


def run_git_empty_fallback(current_file, conflict_path):
    """
    Fallback method: run git merge-file using an empty temp file as the base.
    """
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
                f"    [CONFLICTS] Merged with conflict markers. Please resolve manually inside '{current_file.name}'."
            )
        else:
            print(f"    [ERROR] Git merge-file failed: {result.stderr.strip()}")
    except FileNotFoundError:
        print("    [ERROR] Git is not installed or not in your system PATH.")
    finally:
        if os.path.exists(base_file):
            os.unlink(base_file)


def merge_conflict(sync_root, conflict_path):
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
                # If auto-merge has conflicts, offer to open Meld 3-way
                ans = (
                    input(
                        "    Would you like to open Meld to resolve these conflicts? (y/N): "
                    )
                    .strip()
                    .lower()
                )
                if ans == "y":
                    launch_meld_3way(current_file, base_file, conflict_path)
            else:
                print(f"    [ERROR] Git merge-file failed: {result.stderr.strip()}")
        except FileNotFoundError:
            print("    [ERROR] Git is not installed or not in your system PATH.")
    else:
        # 2. NO common ancestor -> Launch Meld for a 2-way comparison
        print("    - No common ancestor found in .stversions.")
        meld_launched = launch_meld_2way(current_file, conflict_path)

        # If Meld is not installed, fallback to the standard empty-base git merge
        if not meld_launched:
            run_git_empty_fallback(current_file, conflict_path)


def main():
    sync_root = Path(os.getcwd())
    exclude_dirs = {".stversions", ".git"}

    print(f"Scanning for Syncthing conflicts in: {sync_root}")
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
        merge_conflict(sync_root, path)


if __name__ == "__main__":
    main()
