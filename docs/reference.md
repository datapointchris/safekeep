# safekeep reference

The full behaviour, and the reasoning behind it. `README.md` is the short version.

This document moved here from the dotfiles repository in August 2026, when safekeep became its
own project. The decisions it records were made while it lived there.

Config-driven file preservation that rsync-copies files and directories to a destination as dated snapshots. Each snapshot carries a manifest describing what was collected, so a snapshot can be restored without the config that produced it. Zero external dependencies for backup — Python stdlib only. Restore shells out to fzf for interactive selection.

Primary use case: backing up scattered config files, local scripts, and git-untracked WIP from a WSL work machine to a network drive for crash protection, and restoring them onto a rebuilt machine.

## Quick Start

```bash
safekeep                        # Usage. Nothing writes without an explicit verb
safekeep config example         # Read the annotated example without writing anything
safekeep config init            # Write a starter config to ~/.config/safekeep/default.toml
safekeep config show            # Display the resolved config
safekeep config edit            # Open it in $VISUAL/$EDITOR, then report what the edit did
safekeep backup run --dry-run   # Preview what would be copied
safekeep backup run             # Copy the configured paths into today's snapshot
safekeep backup run --tag wip   # Copy only the entries tagged 'wip'

safekeep snapshots list                   # What is at the destination
safekeep snapshots show 2026-08-13        # What one snapshot holds
safekeep tags list                        # Which tags exist, and what each would restore
safekeep tags show wip                    # The sources one tag covers
safekeep restore --to /tmp/restore-test   # Rehearse: pick a snapshot and sources
safekeep restore --to / --tag wip         # Restore everything tagged 'wip'
```

**The verb comes last, and no node acts until one selects it.** That is the no-args-shows-help rule
in `standards/cli-design.md`, and it holds at every level: bare `safekeep`, bare `safekeep backup`
and bare `safekeep config` each print the screen that completes the command line and exit 2. Walking
the tree one token at a time is therefore a way to read it rather than a way to trigger it.

**`backup` is a namespace and `run` is the verb that writes.** Bare `backup` used to copy every
configured path the moment it was typed, which made the exploratory invocation the destructive one —
over a network drive, with no way to ask what it would do short of remembering `--dry-run`. It is
also the arrangement the rule exists to prevent for a second reason: a command that acts bare cannot
gain a sibling without silently changing what the bare form means, and everyone who typed it out of
habit then runs something else.

**`snapshots` and `tags` are namespaces for the same reason, and the change paid for itself
immediately.** `tags wip` was a positional where a `show` belonged, and the two commands feeding
fzf's preview panes — `preview-snapshot` and `preview-source` — were `snapshots show` with the name
left off, hidden because there was no noun to hang them on. Giving `snapshots` a verb slot gave them
one, so the tree now has no undocumented command in it at all.

**`config` was the first of them and is the model.** It was previously a root-level `init` plus a
bare `config` that printed — which meant `config` occupied the noun slot while doing a verb's job,
leaving nowhere for `config init` to go. The rules are in `cli-design.md`: a resource that could ever
grow a second command is a namespace today, and `init` belongs to whatever it initializes. `git init`
creates the tool's entire subject; a config file is one artifact among several, which is why it nests
here and in `go mod init`.

## Config

Config files live at `~/.config/safekeep/<name>.toml`. If only one config exists, it auto-loads. With multiple configs, specify which one with `--config`, which is global and goes before the command: `safekeep --config work backup run`.

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

**Every key states what safekeep will do, so the file reads as a description of the backup rather than a list of this program's variables.** That is the standard in `standards/configuration.md`, and safekeep is its worked example.

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

**Unchanged files are hard links into the previous snapshot** (`rsync --link-dest`), so keeping
every snapshot forever costs only what actually changed. Every snapshot still browses and restores
as a complete tree — there is no chain to walk and no base snapshot that others depend on. The
manifest's `linked_from` names the snapshot the inodes are shared with, or is `null` for a full
copy: the first run, or an rsync without `--link-dest`, which is what macOS ships as
`/usr/bin/openrsync`. Deleting a snapshot directory is always safe; the data survives as long as
any other snapshot links it.

The hazard is the same one the hard links buy the saving with: a shared file is the *same inode*
in every snapshot holding it, so editing one in place edits all of them. Copy out before touching
anything, and never edit inside the destination. Restore only ever reads, so it is unaffected.

## The Manifest

`.safekeep-manifest.json` is written into each snapshot and is what makes it restorable on a machine that no longer has the config. It records the groups collected (kind, source, tags, counts, sizes), the source `home` for remapping, file modes, symlink origins, oversized files that were skipped, any config warnings, and the `label` if one was given.

**A group is a (kind, source) pair, and it is not the unit anything is restored in.** A repo contributes a `git_untracked` group and a `git_ignored` group over one subtree, which restore rsyncs once — so the picker, the counters and the summary all speak in *sources*, and a repo is one row carrying `untracked + ignored`. The manifest keeps the two groups because their file sets are disjoint and each carries its own list; nothing above the manifest has a reason to.

**The git groups record their file lists (`paths`); the path groups do not.** That is what lets a restore say the file it just wrote was gitignored rather than untracked, on a machine where the repo is not present to be asked. A path group's files are all the same kind, so a list there would repeat what its source line already said, at the cost of turning the manifest into a directory listing.

**Modes** are recorded only where they deviate from `0644` for files and `0755` for directories. The destination is typically SMB or DrvFs and cannot store Unix modes, so the backup is written with `--no-perms` and every file arrives with the same mode. Restore applies the recorded deviations, and the defaults only to paths it created, which collapses the map to just the interesting entries — `0600` secrets and executable scripts. Without this, restored SSH and GPG config files come back group-readable and those tools refuse to use them.

**Symlinks** are dereferenced on backup (`rsync --copy-links`) so a snapshot holds real content rather than links that break when the source machine is lost. The manifest records that the source *was* a symlink and where it pointed, so restore can report which restored files should be links and offer `--skip-symlinked`.

A snapshot with no manifest cannot be restored by safekeep — it says so and points at rsync.

## Backup

```bash
safekeep backup run                     # everything the config lists
safekeep backup run --tag secrets       # only the entries carrying that tag
safekeep backup run --source ~/notes    # only the entries whose path contains that string
safekeep backup run --label 'before the wsl move'   # say why this one was taken
```

**Bare `backup` means everything, so `--tag` and `--source` narrow rather than enable.** There is no `--all` to forget, which is the opposite arrangement to restore, where selection is required and never inferred. The asymmetry is deliberate: the failure to design out of a backup is one that silently covers less than was asked for, and the failure to design out of a restore is one that silently covers more.

A tag or path that matches nothing in the config is a usage error rather than a run that copies nothing, because a backup covering nothing reads exactly like one that covered everything it was asked to — the summary only reports what was copied.

### Labels

`--label` writes a free-text note into the snapshot saying why the backup was taken. A date says
*when*, and nothing about *why* — which is exactly the question being asked when an older snapshot is
picked on purpose months later, during a rebuild, by someone who no longer remembers the week.

```bash
safekeep backup run --label 'before moving wsl instance'
```

**safekeep never reads a label.** It is displayed by `snapshots list`, `snapshots show`, the restore
snapshot picker and the restore header, and that is the whole of its behaviour. Tags select and a
label explains, which is why they are two things: a tag is vocabulary the config owns and `--tag`
consumes, and a label is prose about one particular day.

**A later run the same day keeps the label already there.** There is one snapshot per date, so the
routine backup that runs after the risky thing merges into the snapshot taken before it — and
erasing that note would leave the snapshot that matters indistinguishable from every other. An
explicit `--label` replaces, and `--label ''` clears. The mechanism is the manifest key's *presence*:
`do_backup` writes it only when the flag was typed, so an absent flag leaves the key out and the
ordinary manifest merge preserves the earlier value with no special case.

The manifest version does not move for this. It went to 2 for the git groups' file lists, which
*restore* reads and behaves differently for; nothing branches on a label, so a version gate would be
a number no reader checks. Every snapshot taken before this existed has no `label` key and renders
without one — the displays ask with `.get`, and there is no empty column or trailing separator left
where a note would have been.

**A narrowed run merges into the day's snapshot instead of replacing it.** Running `backup --tag secrets` after a full backup on the same day rewrites `.safekeep-manifest.json`, and rsync never deletes, so the files from the earlier run are still sitting in the snapshot. Dropping their groups would leave them on disk and unrestorable, since the manifest is the only record of what a snapshot holds. The merge keeps every group the run did not cover, overlays the modes and symlinks it recorded, and replaces the oversized-file verdicts only for the sources it actually walked.

## Tags

```bash
safekeep tags list               # every tag, the sources it covers, what it would restore
safekeep tags show secrets       # one tag, source by source, and the restore that brings it back
safekeep tags list --from DATE   # size against an older snapshot instead of the newest
```

**A tag lives in two places, and reading either one alone is misleading.** The config says which entries carry it; the manifest inside each snapshot carries a copy of what the config said *on the day that snapshot was taken*. `restore --tag` selects on the manifest, so tagging an entry today does nothing for the snapshots that already exist. `safekeep tags list` reads both sides and marks the disagreements: a tag whose sources are not in the snapshot yet shows `not in this snapshot`, and a tag the config has since dropped or renamed is still listed, because it remains the only name the older snapshots answer to.

That disagreement is the whole reason the command exists. Without it, `restore --to / --tag wsl` reporting `nothing selected` looks like a bug in the tool rather than a snapshot taken before the tag was written.

Sizes come from the snapshot being reported against — the newest restorable one unless `--from` names another — so a tag's row is what a restore would actually bring back rather than what the source paths hold now. Sources carrying no tag at all are counted at the bottom: those are reachable only with `--all` or `--source`, which is worth knowing before a rebuild rather than during one.

## Restore

```bash
safekeep restore --to PATH [--from DATE] [--all | --source PATH | --tag NAME]
                           [--dry-run] [--on-conflict POLICY] [--skip-symlinked]
```

`--to` is required. `--to /` is a real restore; `--to /tmp/restore-test` stages one somewhere harmless, which is how the restore gets rehearsed before it is needed.

**A restore works in sources, not in groups.** A source is one config entry — a path, or one repo's untracked and ignored files together. `--source` was `--group`, which is still accepted and no longer written anywhere: the manifest's groups are an implementation detail of how a repo's two file sets are recorded, and using that word in the output left "restored 39 groups" meaning nothing to the person who had just picked twenty-odd rows out of a picker.

**Selection is always explicit.** With `--all`, `--source`, or `--tag`, restore runs non-interactively. With none of them on a terminal, fzf opens: first a snapshot picker previewing each manifest, then a multi-select source picker previewing the files that source holds, labelled untracked or ignored. With none of them and no terminal, it exits non-zero listing the available sources rather than guessing.

**The source picker is sorted by path**, not in manifest order. Manifest order is config order, which is meaningful to whoever wrote the config and to nobody scanning thirty rows for the one they came for.

Both pickers pin their keys above the prompt — `tab` selects, `shift-tab` deselects, `ctrl-a` takes everything, `enter` restores. Multi-select is fzf's own binding rather than this tool's, so the picker is the only place it can be recalled at the moment it is needed.

**A selection that matched nothing exits 1 and says why.** Cancelling out of the fzf picker is a restore you decided against, and exits 0; `--tag wsl` matching nothing in the snapshot is a request that failed, and a caller has to be able to tell the two apart. The error names the tags that snapshot does carry, which is the fact that distinguishes a typo from a tag added to the config after the snapshot was taken.

Bare `safekeep restore` prints the restore help rather than an error — no args shows help, always. Naming a selection and forgetting `--to` is the other case: intent was stated, so that one is an error naming the single missing option.

### Conflicts

`--on-conflict` chooses what happens when a target file already exists:

| Policy | What it does |
| --- | --- |
| `backup` | Default. Restores, keeping the file it replaced as `<name>.pre-restore` |
| `ask` | Names each existing file and waits — `[y]es [N]o [a]ll [k]eep all [q]uit`. Keeps no copies |
| `skip` | Leaves every existing file alone |
| `overwrite` | Restores over it with nothing kept |
| `newer` | Restores only where the snapshot's copy is the newer one |

**`ask` and `backup` are the two answers to the same question, and which is right depends on how many files are in it.** `backup` never asks and never loses anything, at the cost of a `.pre-restore` file beside everything it replaced — fine for a whole-machine restore, litter for a handful of files. `ask` costs one keystroke per conflicting file and leaves the tree clean, which is unusable at ten thousand files and exactly right at ten. Only conflicts are asked about; a file the target does not have is not a decision. `ask` off a terminal is a usage error rather than a hang, and `q` stops the run where it stands, leaving the sources already restored as they are.

**`backup` and `overwrite` compare by checksum, not by size and timestamp.** rsync's quick check skips a file whose size and mtime match the source without ever reading it, which is right for a sync and wrong for a restore — a file corrupted in place keeps both its size and its timestamp, and that is exactly the file being restored over. Passing `--checksum` makes "the snapshot wins" true by content, and still skips genuinely identical files so no `.pre-restore` copy is manufactured for a file that never changed. `skip` and `newer` keep the metadata comparison, because not touching existing files is what they are for.

### What a restore prints

Each source prints its position, path, size and kinds *before* rsync is called on it, so a wait always belongs to a source that has already been named. Beneath it, **every file is named as rsync writes it**:

```text
  [2/3] ~/code/project        14 files      82 KB  untracked + ignored
      + .planning/status.md   ignored
      ~ scratch.py            untracked   kept .pre-restore copy
      2 files restored · 1 replaced · 11 unchanged
```

`+` is a file the target did not have, `~` one that was already there. Whether it existed is read before rsync runs, which is the only chance to read it — afterwards every path is present and nothing distinguishes the two. The kind comes from the manifest's per-group file lists, so it is only ever shown for a repo's files, where it is the thing worth knowing.

The list is what rsync actually wrote, so files it skipped as identical are counted rather than named. That is also why there is no progress bar: the file lines are the progress signal, and interleaving `--info=progress2`'s redraws with them produced neither. `--out-format=%n` gets the exact list from rsync 3.x, `-v` gets it from the openrsync macOS ships as `/usr/bin/rsync`, and `--outbuf=L` keeps it streaming rather than arriving in 4 KB bursts.

The mode pass prints a counter of its own, that being one `chmod` syscall per restored path and minutes of them over SMB. Redirected output gets every line above and none of the redrawn ones: a carriage return collapses a captured log into a single unreadable line, and a restore is exactly the thing run under `tee`.

**The mode pass touches only what the snapshot holds.** It walks the snapshot's own subtree rather than the target's, applying each recorded deviation and applying the defaults only to paths this restore created. It used to walk the target, which for a repo is the entire working tree — so restoring two untracked files reported eleven thousand paths, and every tracked file got `0644` because the manifest had no mode for a file it had never seen. An executable that a `git clone` had just put back came out non-executable.

A `--dry-run` restore into a target that does not exist yet lists what it would create without calling rsync. rsync will not accept a file destination whose parent directory is missing, and a dry run is not allowed to create one — so for a rehearsal into a fresh directory there is no question left to ask it, since everything under the source would be created.

If the snapshot's home differs from the restoring machine's, paths under it are remapped automatically — a snapshot taken as `/home/chris` restores into whatever `$HOME` is now.

## Key Behaviors

**Idempotent**: Running twice on the same day updates the same dated directory. rsync transfers only changed files.

**Fail fast**: If the destination doesn't exist or isn't writable, exit immediately.

**Smart exclusions**: Default `skip_names_matching` list (`.venv`, `node_modules`, caches) applied to all rsync calls. Override in config.

**Sized from the source**: Totals come from the walk that builds the manifest, not from re-reading the destination, so the backup never stats the whole snapshot back over the network.

## See Also

- [packup](https://datapointchris.github.io/dotfiles/apps/backup/) — timestamped tar+zstd
  archives, the complementary tool
- [Tool Composition](https://datapointchris.github.io/dotfiles/architecture/tool-composition/) —
  how safekeep fits into the wider toolchain
- [pytermstyle](https://github.com/datapointchris/pytermstyle) — the palette and help grammar
