# safekeep

Dated snapshots of the files no package manager will put back.

Rsync-copies configured paths to a destination and writes a manifest into each snapshot recording
what was collected, the source file modes, and which sources were symlinks. That manifest is what
makes a snapshot restorable **without the config that produced it** — the disaster-recovery case,
where the config died with the machine.

The primary destination is a network drive that cannot represent Unix modes. Recording them in the
manifest is the whole point: the copy loses them, and the restore puts them back.

Snapshots are never pruned, and they do not need to be: unchanged files are hard links into the
previous snapshot, so each one costs only what changed while still browsing and restoring as a
complete tree.

## What it is for

Scattered config files, local scripts, and git-untracked work in progress — the things that are not
in a dotfiles repo, not in a package manager, and not in any git remote, and that you only discover
were irreplaceable after they are gone.

## Using it

```bash
safekeep backup                  # copy the configured paths into today's snapshot
safekeep backup --tag work       # tag the snapshot
safekeep snapshots               # list what is at the destination
safekeep tags                    # what each tag covers, and what it would restore
safekeep restore --to ./here     # restore sources from a snapshot
safekeep config example          # print a starting config
```

Bare `safekeep` prints usage. Nothing writes without an explicit verb. `safekeep --help` is the one
copy of the command surface — it is not repeated here.

[`docs/reference.md`](docs/reference.md) is the full behaviour: the manifest format, the restore
conflict policies, the schema-change rules, and the reasoning behind each of them.

## Config

`~/.config/safekeep/<name>.toml`, selected with `-c <name>`. The manifest stays JSON, because
machines write it and humans write the config.

Keys are phrases that state what safekeep will do, so the file reads as a description of the backup
rather than a dump of the program's variables:

```toml
back_up_to = "/Volumes/backup/safekeep"

[[back_up_paths]]
path = "~/.config/nvim"

[git]
repos = ["~/code"]
back_up_untracked_files = true
```

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
task fix        # ruff format and autofix
```

Releases are cut by python-semantic-release from conventional commits on `main`. Nothing is tagged
by hand.

The terminal style — palette, section header, help grammar — comes from
[pytermstyle](https://github.com/datapointchris/pytermstyle), so safekeep's screens are
indistinguishable from the bash and Go CLIs beside it on `PATH`. Everything else is stdlib: the
config is read with `tomllib` and the copying is `rsync`.
