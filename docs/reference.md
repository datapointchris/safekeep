# safekeep reference

The full behaviour, and the reasoning behind it. `README.md` is the short version.

This document moved here from the dotfiles repository in August 2026, when safekeep became its
own project. The decisions it records were made while it lived there.

Config-driven file preservation that rsync-copies files and directories to a destination as dated snapshots. Each snapshot carries a manifest describing what was collected, so a snapshot can be restored without the config that produced it. Zero external dependencies for backup — Python stdlib only. Restore shells out to fzf for interactive selection.

Primary use case: backing up scattered config files, local scripts, and git-untracked WIP from a WSL work machine to a network drive for crash protection, and restoring them onto a rebuilt machine.

## Quick Start

```bash
safekeep                    # Usage. Nothing writes without an explicit verb
safekeep config example     # Read the annotated example without writing anything
safekeep config init        # Write a starter config to ~/.config/safekeep/default.toml
safekeep config show        # Display the resolved config
safekeep config edit        # Open it in $VISUAL/$EDITOR, then report what the edit did
safekeep backup --dry-run   # Preview what would be copied
safekeep backup             # Copy the configured paths into today's snapshot
safekeep backup --tag wip   # Copy only the entries tagged 'wip'

safekeep snapshots                        # What is at the destination
safekeep tags                             # Which tags exist, and what each would restore
safekeep tags wip                         # The sources one tag covers
safekeep restore --to /tmp/restore-test   # Rehearse: pick a snapshot and groups
safekeep restore --to / --tag wip         # Restore everything tagged 'wip'
```

Bare `safekeep` prints usage rather than picking an action, per the no-args-shows-help rule in
`~/dev/standards/cli-design.md`. A tool that did work bare could not gain a second command without
silently changing what the bare invocation means — and here that bare invocation was the one that
wrote. Bare `safekeep config` does the same thing one level down: it names a resource without
selecting a verb, so it prints the namespace's own help and exits 2 rather than guessing `show`.

**`config` is a namespace even though it started with one command.** It was previously a root-level
`init` plus a bare `config` that printed — which meant `config` occupied the noun slot while doing a
verb's job, leaving nowhere for `config init` to go. The rules are in `cli-design.md`: a resource
that could ever grow a second command is a namespace today, and `init` belongs to whatever it
initializes. `git init` creates the tool's entire subject; a config file is one artifact among
several, which is why it nests here and in `go mod init`.

## Config

Config files live at `~/.config/safekeep/<name>.toml`. If only one config exists, it auto-loads. With multiple configs, specify which one with `--config`, which is global and goes before the command: `safekeep --config work backup`.

`safekeep config init` writes a complete annotated starter config, and `safekeep config example` prints the same content to stdout without touching the filesystem — which is what you want when the question is "what does that key look like" rather than "set me up". The shape it produces:

```toml
back_up_to = "/mnt/h/backups"
skip_names_matching = [".venv", "node_modules", "*.pyc"]
skip_files_over_mb = 50

[[back_up_paths]]
path = "~/.ssh"
tags = ["secrets", "rebuild"]

[[back_up_paths]]
path = "~/notes"
tags = ["notes"]

[git]
back_up_untracked_files = true
back_up_ignored_files_matching = ["CLAUDE.md", ".planning"]

[[git.repos]]
path = "~/dotfiles"
tags = ["rebuild"]

[[git.repos]]
path = "~/code/side-project"
tags = ["wip"]
```

**Every key states what safekeep will do, so the file reads as a description of the backup rather than a list of this program's variables.** That is the standard in `~/dev/standards/configuration.md`, and safekeep is its worked example.

**TOML, not JSON, and the reason is `tomllib`.** safekeep takes no dependencies on the backup path because it has to run on a locked-down work machine where installing a package is a fight, and `tomllib` has been in the standard library since 3.11 while YAML has never had a stdlib parser and never will. Comments come free with that choice, and they are what turns the file into its own manual. YAML would additionally have been the wrong fit for a config full of glob patterns: an unquoted `*.pyc` is alias syntax rather than a string, and bare `~` is null.

`tomllib` reads but cannot write, so `init` emits a hand-authored template rather than serializing a dict — which is the better half of the trade, since a serialized dict cannot carry comments. `CONFIG_TEMPLATE` in the script is that file, and two tests assert it parses without warnings and demonstrates repetition rather than one of each key.

**The `[git]` keys must precede the first `[[git.repos]]` block.** TOML closes a table as soon as a subtable opens, so a `back_up_untracked_files` written after the repo blocks is read as part of the last repo. This fails loudly rather than silently, but it is the one ordering constraint the format imposes here.

**Keys:**

- `back_up_to` — base destination path (required, and the only required key)
- `back_up_paths` — absolute paths to copy whole, `~` is expanded (optional)
- `git` — `repos` names the subject; every other key states what is taken from them (optional)
  - `repos` — the git repos themselves
  - `back_up_untracked_files` — copy each repo's untracked files (default `true`)
  - `back_up_ignored_files_matching` — glob patterns matched against the *gitignored* files in those same repos, so `CLAUDE.md` and `.planning` survive a rebuild
- `skip_names_matching` — patterns no backup ever copies (optional, has sensible defaults)
- `skip_files_over_mb` — skip files larger than this many MB (optional)

Every `[[back_up_paths]]` and `[[git.repos]]` block takes a `path` and optional `tags`. Under JSON an entry could also be a bare path string, so there were two shapes to write and two to parse; an array of tables is uniform, and gives every entry a line of its own to be commented on.

**The repo options are nested because they only mean something relative to the repos beside them.** They were once two sibling keys, `git_untracked` and `git_ignored`, which read as two independent lists of things to back up — nothing in the config said the second was a filter scoped to the first, and the answer was only findable in the source. Structure carries that relationship where a name could not, which is why the parent key is a scope and the leaves are statements about it.

**Inside a scope, one key names the subject and the rest state what happens to it.** `repos` is that subject key. An earlier attempt called it `at`, on the theory that a preposition would let the key complete its parent's phrase — "git repos *at* `~/code/project`". It reads only while the parent is adjacent, and is opaque in every error message, doc reference, and line of code that names the leaf alone. A subject is a noun and resists being made to state an action; the rest of the block does that work.

**`back_up_untracked_files` exists even though nothing else can set it to `false`.** Copying untracked files is what the repo block did unconditionally before, and a config that leaves an outcome-shaping default unstated reads as though the tool does nothing but what is written. A key whose value never changes still earns its place when it is the only thing telling the reader what will be copied.

A repo's ignored files are found by set subtraction: `git ls-files --others` (untracked plus ignored) minus `git ls-files --others --exclude-standard` (untracked only), since git has no single flag that lists ignored files without the untracked ones. A pattern matches either the whole repo-relative path or any single component of it, which is why `.planning` catches everything beneath a `.planning/` directory at any depth.

**Tags are labels, not policy.** safekeep never interprets what a tag means — it displays them in the picker and accepts `--tag NAME` as a selector. That keeps scenario knowledge (which paths matter on a rebuild) in the config where it was written, rather than in the tool. The [Tags](#tags) section below covers where a tag lives once a snapshot has been taken.

`tags` must be a list, and a bare `tags = "wsl"` is fatal rather than coerced. Python reads a string as a sequence of characters, so that entry would come out tagged `w`, `s` and `l` — and the only symptom is `restore --tag wsl` selecting nothing from a snapshot whose config plainly carries the tag.

`safekeep config edit` opens the resolved config in `$VISUAL` or `$EDITOR` and reads it back when the editor exits, reporting the warnings and parse errors the edit introduced. It resolves the path without loading it first, because a config that no longer loads is the main reason to open one — loading first would exit before the editor could fix anything. A terminal editor blocks until it closes; a GUI editor returns immediately and the check describes the file as it was, unless it is configured to wait (`EDITOR="code --wait"`).

## Schema Changes

Unrecognized keys warn and are ignored rather than erroring, so a config can be edited ahead of the tool. A missing required key is still fatal.

A generic warning is adequate for a typo but not for a key that used to mean something. Retired keys therefore carry their own message, listed in `RETIRED_KEYS` in the script. Unrecognized keys are also recorded in the snapshot manifest as `config_warnings`, so a snapshot carries evidence that its config was partly ignored when it was taken.

Renamed keys are fatal rather than warned, and are listed separately in `RENAMED_KEYS`. The distinction is whether ignoring the key shrinks the backup: dropping a retired `keep` changes nothing about what gets copied, while ignoring an old `git_untracked` would skip every repo in the config. A run that fails loudly is fixed immediately; a backup that quietly gets smaller is not noticed until a restore needs the files that are not in it.

The config is hand-written and there are few of them, so it has no version field. The manifest is machine-written and outlives tool versions, so it does — and that same split is why the config moved to TOML while the manifest stayed JSON.

A config left behind as `.json` is named rather than reported as absent: `no configs found` is a bewildering thing to read when the file is sitting in the directory. `resolve_config` lists every leftover with the name it should have.

## Destination Structure

A dated subdirectory is created for each day's backup. Full directory structure is preserved from filesystem root, so the origin of every file is unambiguous and restore is a reverse rsync.

```text
/mnt/h/backups/
  2026-08-04/
    .safekeep-manifest.json
    home/chris/
      notes/meeting.md
      .ssh/config
      code/project/scratch.py          (untracked, from git.repos)
    mnt/c/Users/chris/
      Documents/work-notes/report.docx
  2026-08-01/
    ...
```

Path construction: `back_up_to / YYYY-MM-DD / absolute-path-from-root`

**Snapshots are never pruned.** Deciding how many backups to keep is not safekeep's job.

## The Manifest

`.safekeep-manifest.json` is written into each snapshot and is what makes it restorable on a machine that no longer has the config. It records the groups collected (kind, source, tags, counts, sizes), the source `home` for remapping, file modes, symlink origins, oversized files that were skipped, and any config warnings.

**Modes** are recorded only where they deviate from `0644` for files and `0755` for directories. The destination is typically SMB or DrvFs and cannot store Unix modes, so the backup is written with `--no-perms` and every file arrives with the same mode. Restore applies the defaults everywhere and then the recorded deviations, which collapses the map to just the interesting entries — `0600` secrets and executable scripts. Without this, restored SSH and GPG config files come back group-readable and those tools refuse to use them.

**Symlinks** are dereferenced on backup (`rsync --copy-links`) so a snapshot holds real content rather than links that break when the source machine is lost. The manifest records that the source *was* a symlink and where it pointed, so restore can report which restored files should be links and offer `--skip-symlinked`.

A snapshot with no manifest cannot be restored by safekeep — it says so and points at rsync.

## Backup

```bash
safekeep backup                     # everything the config lists
safekeep backup --tag secrets       # only the entries carrying that tag
safekeep backup --group ~/notes     # only the entries whose path contains that string
```

**Bare `backup` means everything, so `--tag` and `--group` narrow rather than enable.** There is no `--all` to forget, which is the opposite arrangement to restore, where selection is required and never inferred. The asymmetry is deliberate: the failure to design out of a backup is one that silently covers less than was asked for, and the failure to design out of a restore is one that silently covers more.

A tag or path that matches nothing in the config is a usage error rather than a run that copies nothing, because a backup covering nothing reads exactly like one that covered everything it was asked to — the summary only reports what was copied.

**A narrowed run merges into the day's snapshot instead of replacing it.** Running `backup --tag secrets` after a full backup on the same day rewrites `.safekeep-manifest.json`, and rsync never deletes, so the files from the earlier run are still sitting in the snapshot. Dropping their groups would leave them on disk and unrestorable, since the manifest is the only record of what a snapshot holds. The merge keeps every group the run did not cover, overlays the modes and symlinks it recorded, and replaces the oversized-file verdicts only for the sources it actually walked.

## Tags

```bash
safekeep tags               # every tag, the sources it covers, what it would restore
safekeep tags secrets       # one tag, source by source, and the restore that brings it back
safekeep tags --from DATE   # size against an older snapshot instead of the newest
```

**A tag lives in two places, and reading either one alone is misleading.** The config says which entries carry it; the manifest inside each snapshot carries a copy of what the config said *on the day that snapshot was taken*. `restore --tag` selects on the manifest, so tagging an entry today does nothing for the snapshots that already exist. `safekeep tags` reads both sides and marks the disagreements: a tag whose sources are not in the snapshot yet shows `not in this snapshot`, and a tag the config has since dropped or renamed is still listed, because it remains the only name the older snapshots answer to.

That disagreement is the whole reason the command exists. Without it, `restore --to / --tag wsl` reporting `nothing selected` looks like a bug in the tool rather than a snapshot taken before the tag was written.

Sizes come from the snapshot being reported against — the newest restorable one unless `--from` names another — so a tag's row is what a restore would actually bring back rather than what the source paths hold now. Sources carrying no tag at all are counted at the bottom: those are reachable only with `--all` or `--group`, which is worth knowing before a rebuild rather than during one.

## Restore

```bash
safekeep restore --to PATH [--from DATE] [--all | --group PATH | --tag NAME]
                           [--dry-run] [--on-conflict POLICY] [--skip-symlinked]
```

`--to` is required. `--to /` is a real restore; `--to /tmp/restore-test` stages one somewhere harmless, which is how the restore gets rehearsed before it is needed.

**Selection is always explicit.** With `--all`, `--group`, or `--tag`, restore runs non-interactively. With none of them on a terminal, fzf opens: first a snapshot picker previewing each manifest, then a multi-select group picker previewing each group's subtree. With none of them and no terminal, it exits non-zero listing the available groups rather than guessing.

Both pickers pin their keys above the prompt — `tab` selects, `shift-tab` deselects, `ctrl-a` takes everything, `enter` restores. Multi-select is fzf's own binding rather than this tool's, so the picker is the only place it can be recalled at the moment it is needed.

**A selection that matched nothing exits 1 and says why.** Cancelling out of the fzf picker is a restore you decided against, and exits 0; `--tag wsl` matching no group in the snapshot is a request that failed, and a caller has to be able to tell the two apart. The error names the tags that snapshot does carry, which is the fact that distinguishes a typo from a tag added to the config after the snapshot was taken.

Bare `safekeep restore` prints the restore help rather than an error — no args shows help, always. Naming a selection and forgetting `--to` is the other case: intent was stated, so that one is an error naming the single missing option.

`--on-conflict` chooses what happens when a target file already exists: `backup` (default, renames the existing file with a `.pre-restore` suffix), `skip`, `overwrite`, or `newer`.

**`backup` and `overwrite` compare by checksum, not by size and timestamp.** rsync's quick check skips a file whose size and mtime match the source without ever reading it, which is right for a sync and wrong for a restore — a file corrupted in place keeps both its size and its timestamp, and that is exactly the file being restored over. Passing `--checksum` makes "the snapshot wins" true by content, and still skips genuinely identical files so no `.pre-restore` copy is manufactured for a file that never changed. `skip` and `newer` keep the metadata comparison, because not touching existing files is what they are for.

**A restore reports as it goes, because that checksum comparison makes it slow enough to look hung.** Each source prints its position, its path, and the size the manifest recorded for it *before* rsync is called on it, so a wait always belongs to a group that has already been named. On a terminal rsync adds a live line beneath it — `--info=progress2` where the rsync supports it, `--progress` on the openrsync that macOS ships as `/usr/bin/rsync` — and the mode-reapplication pass prints a counter of its own, that being one `chmod` syscall per restored path and minutes of them over SMB. Redirected output gets the per-group lines and none of the redrawn ones: a carriage return collapses a captured log into a single unreadable line, and a restore is exactly the thing run under `tee`.

A `--dry-run` restore into a target that does not exist yet reports what it would create without calling rsync for those groups. rsync will not accept a file destination whose parent directory is missing, and a dry run is not allowed to create one — so for a rehearsal into a fresh directory there is no question left to ask it, since everything under the group would be created.

If the snapshot's home differs from the restoring machine's, paths under it are remapped automatically — a snapshot taken as `/home/chris` restores into whatever `$HOME` is now.

## Key Behaviors

**Idempotent**: Running twice on the same day updates the same dated directory. rsync transfers only changed files.

**Fail fast**: If the destination doesn't exist or isn't writable, exit immediately.

**Smart exclusions**: Default `skip_names_matching` list (`.venv`, `node_modules`, caches) applied to all rsync calls. Override in config.

**Sized from the source**: Totals come from the walk that builds the manifest, not from re-reading the destination, so the backup never stats the whole snapshot back over the network.

## See Also

- [backmeup](https://datapointchris.github.io/dotfiles/apps/backmeup/) — timestamped tar+zstd
  archives, the complementary tool
- [Tool Composition](https://datapointchris.github.io/dotfiles/architecture/tool-composition/) —
  how safekeep fits into the wider toolchain
- [pytermstyle](https://github.com/datapointchris/pytermstyle) — the palette and help grammar
