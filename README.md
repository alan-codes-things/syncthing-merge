# syncthing-merge

Safely merge Syncthing `.sync-conflict-*` files.

`syncthing-merge` finds conflict files, tries a real 3-way merge using Git or
Syncthing version history, opens your configured Git merge tool for anything
that still overlaps, validates the result, backs everything up, and only then
atomically replaces the live file.

It is designed for people who have lost data to an over-eager conflict cleaner.
**A conflict is never deleted just because a tool exited or because you answered
"yes".** If anything looks wrong, the live file and the conflict file are left
alone.

---

## What changed from 0.1

The old version had several data-loss bugs:

- It ran `git merge-file` directly on the live current file, so Git wrote
  conflict markers into the file you were syncing.
- It mapped `$MERGED` to the live current file, so a GUI merge tool could write
  directly into it.
- It treated **any** non-zero exit from the merge tool as success, including
  exit code 127 ("command not found").
- It deleted the conflict file after a `y/N` prompt, without checking whether
  the result was valid or still contained conflict markers.
- Its Meld argument order did not match Git's, so the panes were confusing.
- Its 2-way fallback had no output file: editing the conflict pane and then
  deleting it could lose the edits.

Version 0.2 fixes these by working only on temporary copies, validating every
result, and backing up before writing.

---

## Safety model

1. **Live files are never merge inputs.** `LOCAL`, `BASE`, `REMOTE`, and
   `MERGED` are temporary copies.
2. **`git merge-file -p` is used**, which prints the result to stdout and does
   not modify its input files.
3. **The result is validated** before installation. The tool refuses:
   - any new Git conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`, `|||||||`);
   - an empty result that would unexpectedly wipe a non-empty file;
   - a result identical to the base when a side changed;
   - a `MERGED` file the GUI tool never changed;
   - a GUI tool that modified `LOCAL`, `BASE`, or `REMOTE` instead of
     `MERGED`;
   - a merge tool that exits non-zero.
4. **Everything is backed up first.** The current file, the conflict file, the
   base, and the installed result are copied to a timestamped backup directory
   outside the sync root by default.
5. **Installation is atomic.** The result is written to a temporary
   `~syncthing~*.tmp` file in the target directory, flushed, and then renamed
   over the live file. A crash cannot leave a half-written file.
6. **Inputs are re-checked immediately before installation.** If Syncthing
   changes the live file or the conflict file while the merge tool or backup is
   running, the tool refuses to overwrite or delete it.
7. **The conflict file is removed only after a validated install.** If any step
   fails, it stays where it is.

If a merge tool somehow changes a live file while it is open, the tool stops and
restores the live file from the pre-merge backup when the change contains
conflict markers. Otherwise it leaves the external change alone and reports it.

---

## Pane layout: always resolve in MERGED

The default path is always a **3-way merge** with an explicit output file:

```
LOCAL  |  BASE  |  REMOTE   ->   MERGED / Result / Output
```

Resolve conflicts in the **MERGED / Result / Output** pane, then save and close.
Do not edit `LOCAL`, `BASE`, or `REMOTE`; those are read-only inputs in the
temporary directory. The tool detects and refuses changes to them.

| Tool | What opens | Where to resolve |
| --- | --- | --- |
| Meld | `LOCAL`, `BASE`, `REMOTE` with an output file | The **MERGED / output** pane |
| VS Code | 3-way merge editor | The **Result** pane |
| KDiff3 | `BASE`, `LOCAL`, `REMOTE` with `-o MERGED` | The **MERGED / output** pane |
| vimdiff | `LOCAL`, `BASE`, `REMOTE`, `MERGED` | The `MERGED` buffer |
| Others | Git-compatible `$LOCAL`/`$BASE`/`$REMOTE`/`$MERGED` | The `$MERGED` file |

If you use `--two-way` (opt-in), there is no `MERGED` pane. In that mode, edit
the **LOCAL** copy; only `LOCAL` is installed. Editing `REMOTE` is detected and
refused.

---

## Installation

Requires Python 3.10+ and Git.

```bash
# With uv
uv tool install --reinstall .

# Or with pipx
pipx install --force .
```

For development:

```bash
python -m unittest discover -s tests -v
ruff format src tests
ruff check src tests
PYTHONPATH=src python -m syncthing_merge --help
```

---

## Quick start

```bash
cd ~/Sync
syncthing-merge
```

The default run:

1. scans the current directory (skipping `.git` and `.stversions`);
2. tries automatic 3-way merges;
3. opens your Git merge tool for each remaining conflict;
4. validates, backs up, installs, and removes resolved conflict files;
5. prints a summary.

Useful first commands:

```bash
syncthing-merge --list                 # what conflicts are here?
syncthing-merge --doctor               # is Git/tool/display/backup ready?
syncthing-merge --dry-run              # what would happen?
syncthing-merge --auto                 # automatic merges only, no GUI
syncthing-merge --require-clean        # refuse to run on a dirty Git repo
```

The tool accepts a path:

```bash
syncthing-merge ~/Sync
syncthing-merge ~/Sync --list
```

---

## Commit before merging

If the sync root is inside a Git worktree, `syncthing-merge` checks for
uncommitted tracked changes and prints a warning:

```
[!] WARNING: Git has uncommitted tracked changes.
    Commit or stash them before merging so you can recover.
```

Commit or stash first. If you want the tool to **refuse** to run instead of
warning, use:

```bash
syncthing-merge --require-clean
```

Untracked files (including the conflict files themselves) do not count as
dirty. The warning is about tracked work you could lose.

---

## How the 3-way base is chosen

For each conflict, the tool tries these sources in order:

1. **Git history** — the last commit at or before the conflict timestamp that
   touched the original path. This is usually the best base.
2. **Syncthing version history** — the newest file in `.stversions` whose
   timestamp is at or before the conflict timestamp. Both current
   (`stem~stamp.ext`) and legacy (`original~stamp`) names are supported.
3. **An empty base** — used for a 3-way merge with no common ancestor. This is
   safer than a 2-way pane because it still gives you an explicit `MERGED`
   output.

Disable sources with `--no-git-base`, `--no-stversions`, or force an empty base
with `--empty-base`. Use `--versions-dir NAME` if your Syncthing versioning
folder is not `.stversions`.

If you really want the old-style 2-way comparison, use `--two-way`. It edits a
temporary `LOCAL` copy and installs only that copy.

---

## Backups and recovery

By default, backups go outside the sync root:

| Platform | Default backup root |
| --- | --- |
| Linux | `$XDG_STATE_HOME/syncthing-merge/backups` or `~/.local/state/syncthing-merge/backups` |
| macOS | `~/Library/Application Support/syncthing-merge/backups` |
| Windows | `%LOCALAPPDATA%\syncthing-merge\backups` |

Each sync root gets a slug and short hash, and each run gets a timestamp:

```
<backup-root>/<sync-root-slug>-<hash>/<YYYYMMDD-HHMMSS>/
    MANIFEST.txt
    before/<relative-path>       # the live file before the merge
    conflicts/<conflict-name>    # the .sync-conflict file
    base/<relative-path>         # the 3-way base, if there was one
    merged/<relative-path>       # exactly what was installed
    restored/<relative-path>     # only for --restore-missing
```

To recover, copy a file back from `before/` or `merged/`:

```bash
cp -a "<backup>/before/path/to/file" ~/Sync/path/to/file
```

Use `--backup-dir PATH` to choose a different parent. Backups inside the sync
root are allowed but discouraged, because Syncthing will sync them too; the tool
prints a warning if you do that.

`--no-backup` disables backups. In a script you must also pass `--yes`; without
a TTY the tool refuses rather than silently skipping backups.

---

## Handling many conflicts

The default run is batch-oriented: no per-file "continue?" prompts. Clean
merges are installed automatically; overlapping ones open the visual tool one
after another. Each result is backed up and validated independently.

For a completely non-interactive run that only does automatic merges:

```bash
syncthing-merge --auto
```

Unresolved conflicts remain in place, and the exit code is `2`. Re-run later
after resolving them, or run without `--auto` to open the GUI tools.

To leave conflict files in place after a successful merge (for review):

```bash
syncthing-merge --keep-conflicts
```

---

## Restoring a missing original

A conflict can exist when the original file was deleted on this device. The
default behavior is to **keep the conflict and do nothing**:

```text
[!] original file is missing; conflict preserved.
    Use --restore-missing to restore it from the conflict file.
```

If you want the conflict content to become the original again:

```bash
syncthing-merge --restore-missing --yes
```

This backs up the conflict, writes it to the original path atomically, and
removes the conflict file (unless `--keep-conflicts`). Marker-containing
conflict files are refused unless `--allow-input-markers` is also given.

---

## Binary files, symlinks, and markers

- **Binary files** (NUL bytes in the first 8 KiB) are not auto-merged. Both
  files are left untouched and the conflict is reported. Resolve those
  manually.
- **Symlinks** are refused. The tool never follows a symlink conflict or a
  symlink original.
- **Files that already contain marker-like lines** (for example a Markdown file
  with a literal `=======`) can be merged with
  `--allow-input-markers`. This only permits marker lines that were already
  present in `LOCAL`, `BASE`, or `REMOTE`; newly introduced markers are still
  refused.

---

## Merge tool integration

The tool reads your Git configuration, just like `git mergetool`:

- `merge.tool` selects the default tool.
- `mergetool.<tool>.cmd` overrides the 3-way command.
- `difftool.<tool>.cmd` overrides the 2-way command used by `--two-way`.

Built-in commands exist for Meld, VS Code (`vscode`/`code`), KDiff3, P4Merge,
Beyond Compare (`bc`/`bc3`/`bc4`), vimdiff, and opendiff. The Meld, VS Code, and
KDiff3 argument orders match Git's own mergetools.

Custom commands receive the standard environment variables `$LOCAL`, `$BASE`,
`$REMOTE`, and `$MERGED`. Use `--tool NAME` to force a configured tool for one
run. `--doctor` shows which command will be used.

Examples:

```bash
git config --global merge.tool meld
git config --global mergetool.meld.cmd 'meld --auto-merge --output="$MERGED" "$LOCAL" "$BASE" "$REMOTE"'
```

---

## All options

| Option | Meaning |
| --- | --- |
| `PATH` | Sync root to scan (default: current directory). |
| `--tool NAME` | Use this Git mergetool name instead of `merge.tool`. |
| `--auto` | Only automatic merges; never open a visual tool (same as `--no-open`). |
| `--no-open` | Never open a visual merge tool. |
| `--yes`, `-y` | Never prompt; required with `--no-backup` in scripts. |
| `--dry-run` | Show what would happen without changing anything. |
| `--list` | List conflicts and exit. |
| `--doctor` | Check Git, merge tool, display, and backup setup. |
| `--require-clean` | Abort if the Git worktree has uncommitted tracked changes. |
| `--force` | Allow suspicious empty/base-equal results (never conflict markers). |
| `--no-backup` | Disable backups (dangerous). |
| `--backup-dir PATH` | Parent directory for backups. |
| `--keep-conflicts` | Keep `.sync-conflict` files after resolving them. |
| `--restore-missing` | Restore a missing original from its conflict file. |
| `--two-way` | Force 2-way mode: edit the LOCAL copy; only LOCAL is installed. |
| `--empty-base` | Use an empty 3-way base instead of Git/`.stversions` history. |
| `--no-auto-merge` | Skip `git merge-file` and open the visual tool directly. |
| `--no-stversions` | Do not use `.stversions` history as a base. |
| `--no-git-base` | Do not use Git history as a base. |
| `--allow-input-markers` | Allow marker lines that already exist in the inputs. |
| `--versions-dir NAME` | Syncthing versions directory (default: `.stversions`). |
| `--exclude GLOB` | Skip paths matching GLOB (repeatable). |
| `--verbose`, `-v` | Show base selection and tool commands. |
| `--version` | Print the version. |
| `--help` | Print help. |

---

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | All conflicts resolved (or none found), or a dry run completed. |
| `2` | Some conflicts remain unresolved, skipped, or failed validation. |
| `1` | Fatal error: bad path, dirty repo with `--require-clean`, setup problem. |
| `130` | Interrupted with Ctrl-C. |

`--list` exits `0` when it can list. `--doctor` exits `0` when no problems are
detected and `1` otherwise.

---

## Troubleshooting

**"no visual merge tool detected"**

Set one explicitly:

```bash
git config --global merge.tool meld
# or
syncthing-merge --tool vscode
```

Run `syncthing-merge --doctor` to see whether the executable is on `PATH`.

**The GUI does not open**

On Linux, check `DISPLAY` or `WAYLAND_DISPLAY`. `--doctor` reports whether a
display is set. On a headless machine, use `--auto` for clean merges only, or
configure a terminal tool such as `vimdiff`.

**"the merge tool modified LOCAL/BASE/REMOTE"**

You edited one of the input panes instead of the `MERGED`/Result/Output pane.
The live file was not changed. Re-run and save your resolution to `MERGED`.
Temporary files are kept and their path is printed for recovery.

**"the result still contains Git conflict markers"**

The merge tool saved an unresolved file. Re-open the conflict and finish the
resolution in the `MERGED` pane. The live file was not changed.

**"the result is empty even though the inputs are not"**

The tool refused a suspicious empty result. If the empty result is intentional,
re-run with `--force`. Otherwise recover the previous file from the `before/`
backup.

**"the result is identical to the base even though a side changed"**

The result would discard both sides' changes. This is almost always a tool or
pane mistake. If it is intentional, use `--force`.

**A backup directory is inside the sync root**

Syncthing will sync the backups too. Prefer the default location or
`--backup-dir` outside the sync root.

**A conflict file is kept after a successful merge**

You passed `--keep-conflicts`, or the conflict file changed or could not be
removed. With `--keep-conflicts` this is intentional and the run exits `0`.
Otherwise the merged file is installed, the conflict is kept, and the run exits
`2` so scripts notice. Verify the merged file, then delete the leftover
conflict manually.

---

## Limitations

- Binary conflicts are never merged automatically.
- Symlink conflicts are never touched.
- If there is no usable Git or Syncthing history, the tool uses an empty base;
  overlapping text still needs a human.
- Git history reflects commits, not uncommitted work. That is why the tool warns
  you to commit before merging.
- A GUI merge tool must be able to write the `$MERGED` file. Tools that only
  edit `$LOCAL` are supported through the explicit `--two-way` mode.

---

## License

See [LICENSE](LICENSE).
