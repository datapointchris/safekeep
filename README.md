# safekeep

Timestamped snapshots of the files no package manager will put back.

safekeep rsync-copies the paths a config names to a destination, one snapshot per run. Each
snapshot carries a manifest. It records what was collected, which sources were symlinks, and the
file modes the destination cannot keep. The manifest makes a snapshot restorable **without the
config that produced it**. That is the disaster-recovery case, where the config died with the
machine.

It is for scattered config files, local scripts and git-untracked work in progress. None of them is
in a dotfiles repo, a package manager or a git remote. You learn they were irreplaceable after they
are gone. It is not a whole-disk backup: it copies what the config names and nothing else.

The destination is typically a network drive that cannot store Unix modes. The copy loses them. The
manifest records them for a restore to put back. The copies are plain files, neither compressed nor
encrypted. A file browser reads a snapshot, and so does anyone who can read the drive.

[`docs/reference.md`](docs/reference.md) is the full behavior: the config schema, the snapshot
layout, the manifest format, the restore conflict policies, the schema-change rules, and the
reasoning behind each of them.

## Install

safekeep installs as a uv tool, from a tagged release. uv fetches the Python it needs.

```bash
tag=$(git ls-remote --tags --refs --sort=-v:refname https://github.com/datapointchris/safekeep 'v*' | head -1 | cut -d/ -f3)
uv tool install "git+https://github.com/datapointchris/safekeep@$tag"   # the newest release
safekeep --version                                                      # the release that installed
safekeep update --check                                                 # whether a newer one exists
```

`safekeep update` installs the newest release over the running one. It refuses an install from the
default branch, because a branch carries no release version to compare. Install from a tag to keep
it updatable. Every other command checks for a newer release at most once a day, and names one it
finds in a line on stderr.

safekeep runs `rsync` for every copy and `git` for repo sources. It needs `fzf` only for the
interactive restore pickers.

## Starting from scratch

```bash
safekeep config init          # the annotated example as ~/.config/safekeep/default.toml; never overwrites one
safekeep config edit          # set back_up_to, and replace the example sources with your own
safekeep backup run -n        # what a backup would copy; writes nothing, not even the destination
safekeep backup run           # the first snapshot, named for the second it started
safekeep snapshots list       # the snapshot at the destination: its size, files, sources and machine
safekeep restore --to ~/.cache/safekeep/rehearsal --all   # proves it restores, into a scratch directory
```

`config init` writes the annotated example, example sources included, so the edit is not optional.
A config that already exists is left alone, and the refusal names `config edit` instead.
`safekeep config example` prints the same file without writing it.

`config edit` opens `$VISUAL` or `$EDITOR` and checks the file when the editor exits. A GUI editor
returns at once, so the check reads the file as it was unless the editor waits
(`EDITOR="code --wait"`).

`backup run -n` checks that the destination is writable, or that its nearest existing parent is,
and creates nothing. A source that does not exist is named `skip: … (not found)`, and the run goes
on without it.

The rehearsal restores into `$XDG_CACHE_HOME/safekeep/rehearsal`, which is
`~/.cache/safekeep/rehearsal` when the variable is unset. Deleting it costs nothing. A real restore
is the same command with `--to /`.

The second backup says whether hard links work on this destination. Its last line reads
`unchanged files: hard links into <snapshot>`, or
`unchanged files: copied in full, linked to no earlier snapshot`. The first backup always reads
`copied in full`, because there is no earlier snapshot to link to.

## The grammar

The noun comes first and the verb last: `safekeep snapshots list`, not `safekeep list snapshots`.
Nothing acts until a verb selects it. Bare `safekeep`, bare `safekeep backup` and bare
`safekeep config` each print the help that completes the line, and exit 2. So walking the tree one
word at a time reads it rather than triggering a backup.

`-c NAME` picks a config when there are several, and goes before the command. A verb's options go
after the verb. An option on the wrong side fails with the corrected line printed:
`safekeep snapshots list -c work` answers with `safekeep -c work snapshots list`.

Every read takes `--json`. Errors go to stderr, prefixed `safekeep:`, and a miss exits 1. A usage
error exits 2. Every command safekeep prints in a hint runs as printed, with `-c` added where there
are several configs.

The root help groups the commands in the order a backup is used:

- **Back up** — `backup`. The only command that writes to the destination.
- **Read** — `snapshots`, `files`, `tags`. Each reads the destination, and none writes.
- **Restore** — `restore`. Writes under `--to`, and reads one snapshot.
- **Manage** — `config`, `update`.

## Which surface answers which question

| Ask | Surface | Reads |
| --- | --- | --- |
| Which config is in use, and what it backs up | `config show` | the config directory, and the file it picks |
| What would a backup copy | `backup run -n` | the config and each source, writing nothing |
| Take a snapshot | `backup run` | the config and each source, writing one snapshot |
| What is on the drive | `snapshots list`, `snapshots show` | each snapshot's manifest |
| What did an older machine have that this one lacks | `files list --missing` | every snapshot's tree, against this machine's home |
| What would a restore by tag bring back | `tags list`, `tags show` | the config, and one snapshot's manifest |
| Get files back | `restore` | one snapshot's manifest and files |
| Is there a newer release | `update --check` | this repository's GitHub releases |

## `config` — one TOML file per config, whose keys describe the backup

```bash
safekeep config show          # the file in use, its destination and sources, and any warnings
safekeep config example       # every key, annotated, written nowhere
safekeep config init work     # a second config, which -c work then reads
```

Each config is one TOML file in `$XDG_CONFIG_HOME/safekeep`, or `~/.config/safekeep` when the
variable is unset, named for the config. With one config, every command reads it. With several,
`-c NAME` picks one. `-c` also takes the path of a `.toml` file kept anywhere else.

Keys are phrases that state what safekeep will do. So the file reads as a description of the backup
rather than a dump of the program's variables:

```toml
back_up_to = "/Volumes/backup/safekeep"

[[back_up_paths]]
path = "~/.ssh"
tags = ["secrets"]

[[back_up_paths]]
path = "~/.config/nvim"

[git]
back_up_untracked_files = true
back_up_ignored_files_matching = ["*.env"]

[[git.repos]]
path = "~/code/side-project"
tags = ["wip"]
```

A `[[git.repos]]` block backs up a repo's untracked files. A clone brings back everything else.
`back_up_ignored_files_matching` adds the gitignored files matching a pattern, such as a stray
`.env`. The `[git]` keys must come before the first `[[git.repos]]` block, because TOML closes a
table as soon as a subtable opens.

The manifest is JSON, because machines write it. The config is TOML, because people write it.

An unknown key warns and is ignored, so a config can be edited ahead of the tool. The snapshot's
manifest records the warning. A retired key warns with its own message. A renamed key is fatal,
because ignoring it would back up less than the config asks for. A backup that quietly shrinks goes
unnoticed until a restore needs the missing files.

## `backup run` — one snapshot per run, and none is ever pruned

```bash
safekeep backup run -n                              # what it would copy, writing nothing
safekeep backup run                                 # every source the config lists
safekeep backup run --tag secrets                   # only the sources tagged secrets
safekeep backup run --label 'before the wsl move'   # say why this one was taken
```

Every run writes its own snapshot, named for the second it started. A file created and then mangled
between two runs on one day keeps its good version in the earlier snapshot. One snapshot per day
would overwrite that copy, and no older snapshot would hold it. Two runs inside one second share a
name, and the second run's manifest is merged into the first's.

Unchanged files are hard links into the previous snapshot. Each snapshot costs only what changed,
and still browses and restores as a complete tree. No snapshot depends on another, so deleting one
by hand is safe. A linked file is the same inode in every snapshot holding it, so editing one in
place edits all of them. Copy a file out before changing it.

A hard link is made only where rsync lists `--link-dest` in its help and the destination accepts the
link. A destination can refuse one, and rsync then copies the file in full and still exits 0. So the
manifest records the sharing it observed, by comparing inodes after the copy. The run's last line
and `snapshots show` both print that verdict. A run of snapshots reading `copied in full` means each
one costs its full size.

`--tag` and `--source` narrow the run, so there is no `--all` to forget. A narrowed run writes a
partial snapshot beside the full one rather than topping it up, and `snapshots list` marks it
narrowed. So the newest snapshot is not always the most complete one. A tag or source that matches
nothing in the config is a usage error rather than a run that copies nothing.

`--label` is free text kept in the snapshot. `snapshots list`, `snapshots show` and the restore
picker print it. A date says when a snapshot was taken. A label says why. safekeep never reads a
label.

## `snapshots` — every run at the destination, read from its manifest

```bash
safekeep snapshots list                             # newest first: size, files, sources, machine, label
safekeep snapshots show 2026-08-13                  # that day's last run: its sources, sizes and tags
safekeep snapshots show 2026-08-13 --source ~/.ssh  # the files that run holds for one source
```

A snapshot is a directory named like `2026-08-04T17-32-08`. It holds the copied files at their full
path from the root, beside `.safekeep-manifest.json`. The name has no colon, because NTFS forbids one
in a filename. A date selects too: `--from 2026-08-13` is the last run of that day.

A snapshot missing its manifest is listed and marked, because safekeep cannot restore it. A restore
from one prints the `rsync` command that copies it out without the modes.

## `files` — every file once, under the newest snapshot holding it

```bash
safekeep files list --missing                                     # what older snapshots hold that this machine lacks
safekeep files list --from 2026-08-04                             # everything one snapshot holds, flat
safekeep restore --to / --from 2026-08-04 --source ~/.gitconfig   # bring one back from the snapshot it is listed under
```

Each file is listed once, under the newest snapshot that holds it. That is the copy a restore brings
back. A file six directories deep is still one line.

Each backup copies only what the machine running it has. So a file an older machine had, and this
one never got back, is only in the older snapshots. `--missing` is the one view that reads across
snapshots and finds it. "On this machine" is read against this machine's home, so a snapshot taken
under another username is compared where a restore would put its files.

A restored file is backed up again only if a source in the config covers its path.

## `tags` — a restore reads the tags a snapshot recorded, not the config's

```bash
safekeep tags list            # every tag, and what a restore by it would bring back
safekeep tags show secrets    # one tag, source by source, and the restore that brings it back
```

A tag is a name given to sources in the config, such as `secrets`. Tag by the scenario you would
restore in, not by what the files are. safekeep never interprets a tag.

Each snapshot keeps a copy of the tags its config had when it was taken, and `restore --tag` selects
on that copy. So tagging a source today does nothing for the snapshots that already exist. `tags`
reads the config and one snapshot together, and marks where they differ. Without it, a restore by
tag that selects nothing looks like a bug rather than a snapshot older than the tag.

## `restore` — nothing is restored until you name it

```bash
safekeep restore --to ~/.cache/safekeep/rehearsal --tag secrets   # rehearse into a scratch directory
safekeep restore --to / --tag secrets                             # then restore for real
safekeep restore --to ~/.cache/safekeep/rehearsal                 # on a terminal: pick a snapshot, then sources
```

A restore needs `--to`, and one of `--all`, `--source PATH` or `--tag NAME`. It never infers a
selection. A backup starts from everything and narrows, and a restore starts from nothing. On a
terminal with no selection, it lists the snapshots to choose from, then the sources in the one
chosen. Off a terminal it exits 1 and lists the sources the snapshot holds. A selection that matches
nothing exits 1 too.

`--to /` puts files back where they were. Files backed up from under the old machine's home land
under this machine's home, whatever the user is called. A file already at the target is replaced and
kept beside it as `<name>.pre-restore`, unless `--on-conflict` names another policy. The modes the
manifest recorded are applied after the copy, so a `0600` key comes back `0600`.

Without `--from`, a restore reads the newest snapshot. Where a narrowed backup took that one, the
restore brings back what it holds of the selection. It then prints a restore for each missing
source, from the newest snapshot holding it.

## A new machine needs only the drive

Each snapshot's manifest records what it holds, its tags and the modes to restore. So a config
holding only `back_up_to` reads every snapshot on the drive:

```bash
safekeep config init                  # then set back_up_to to the drive and delete the sources
safekeep snapshots list               # what the drive holds
safekeep tags list                    # what each tag would bring back from the newest snapshot
safekeep restore --to ~/.cache/safekeep/rehearsal --tag secrets
safekeep restore --to / --tag secrets
safekeep files list --missing         # what the snapshots hold that this machine still lacks
```

`back_up_to` is the directory holding the snapshots.

## Development

```bash
task setup      # the virtualenv, from uv.lock
task test       # pytest
task lint       # ruff, mypy and bandit, writing nothing
task format     # ruff format and autofix
```

python-semantic-release cuts each release from the conventional commits on `main`, and the release
tag is the version. Nothing is tagged by hand.

[Typer](https://typer.tiangolo.com) owns the command tree and renders the help.
[pytermstyle](https://github.com/datapointchris/pytermstyle) colors the command output and clips
its rows to the terminal. pyselfupdate is the once-a-day release check and `safekeep update`. The
config is read with stdlib `tomllib`, and the copying is `rsync`.
