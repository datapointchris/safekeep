# safekeep

Timestamped snapshots of the files no package manager will put back.

Rsync-copies configured paths to a destination and writes a manifest into each snapshot recording
what was collected, the source file modes, and which sources were symlinks. That manifest is what
makes a snapshot restorable **without the config that produced it** — the disaster-recovery case,
where the config died with the machine.

The primary destination is a network drive that cannot represent Unix modes. Recording them in the
manifest is the whole point: the copy loses them, and the restore puts them back.

Every run writes its own snapshot, named for the second it started. A file created and mangled
between two runs on one day therefore keeps its good version, which a per-day snapshot lost by
overwriting the only copy.

Snapshots are never pruned. Unchanged files are hard links into the previous run, so each snapshot
costs only what changed while still browsing and restoring as a complete tree.

## What it is for

Scattered config files, local scripts, and git-untracked work in progress — the things that are not
in a dotfiles repo, not in a package manager, and not in any git remote, and that you only discover
were irreplaceable after they are gone.

## Using it

```bash
safekeep config init                # write a first config, then set back_up_to and the sources
safekeep backup run -n              # what a backup would copy, before it copies anything
safekeep backup run                 # copy every source into a new snapshot
safekeep backup run --label 'before the wsl move'   # say why this one was taken
safekeep snapshots list             # the snapshots at the destination, newest first
safekeep files list --missing       # what older snapshots hold that this machine lacks
safekeep tags list                  # what each tag covers, and what a restore by it brings back
safekeep restore --to ~/.cache/safekeep/rehearsal --tag secrets   # rehearse into a scratch directory
safekeep restore --to / --tag secrets                             # then restore for real
```

A restore names what it brings back: `--all`, `--source PATH` or `--tag NAME`. Run on a terminal
without one, it lists the snapshots to choose from, then the sources in the one chosen.

The help screens are the one copy of the command surface: `safekeep --help` for the tree, and each
command's `-h` for its flags. So it is not repeated here.

## Restoring onto a new machine

Each snapshot's manifest records what it holds, its tags and the modes to restore. So a config
holding only `back_up_to` reads every snapshot on the drive:

```bash
safekeep config init                 # then set back_up_to to the drive and delete the sources
safekeep snapshots list              # what the drive holds
safekeep restore --to ~/.cache/safekeep/rehearsal --tag secrets
safekeep restore --to / --tag secrets
```

Files backed up from under the old machine's home land under this machine's home, whatever the
user is called. `safekeep files list --missing` then shows what the snapshots hold that this
machine still lacks.

[`docs/reference.md`](docs/reference.md) is the full behavior: the manifest format, the restore
conflict policies, the schema-change rules, and the reasoning behind each of them.

## Config

Each config is one TOML file in the XDG config directory, named for the config. `-c NAME` picks one
when there are several, and `-c` also takes the path of a `.toml` file kept anywhere else.
`safekeep config show` names the file in use. The manifest stays JSON, because machines write it
and humans write the config.

Keys are phrases that state what safekeep will do, so the file reads as a description of the backup
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

[[git.repos]]
path = "~/code/side-project"
tags = ["wip"]
```

`safekeep config example` prints every key with what it does.

Retired keys carry their own message rather than a generic "unknown key" warning — a typo is
harmless noise, but a key whose removal silently shrinks the backup is not.

## Installing

```bash
uv tool install git+https://github.com/datapointchris/safekeep
```

## Development

```bash
task test       # pytest
task lint       # ruff, mypy, bandit
task format     # ruff format and autofix
```

Releases are cut by python-semantic-release from conventional commits on `main`. Nothing is tagged
by hand.

[Typer](https://typer.tiangolo.com) owns the command tree and renders the help.
[pytermstyle](https://github.com/datapointchris/pytermstyle) colors the command output and clips
its rows to the terminal, so a run reads like the bash and Go CLIs beside it on `PATH`. The config
is read with stdlib `tomllib`, and the copying is `rsync`.
