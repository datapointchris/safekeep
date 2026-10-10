# safekeep

Timestamped snapshots of the files no package manager will put back. `backup run` copies a
config's sources into a new snapshot, and `restore` copies a snapshot's sources back with the
modes the destination could not keep. `docs/index.md` routes to the reference, which holds the
config schema, the manifest format and the reasoning behind each behavior.

## Two modules: the command tree, and the logic it calls

`src/safekeep/main.py` is the typer app. Each command resolves the config and hands the logic
typed parameters or a request dataclass, `BackupRequest` or `RestoreRequest`. A renamed flag then
fails mypy rather than a run. `src/safekeep/__init__.py` holds the logic and prints its output.
`tests/test_safekeep.py` is the one test module.

## The manifest is the only record of what a snapshot holds

- **A snapshot restores without the config that wrote it.** Whatever a restore needs goes in the
  manifest: sources, tags, kinds, modes, symlinks, and the home the snapshot was taken under. A
  restore by tag selects on the snapshot's tags, never the config's.
- **Modes travel in the manifest, not the copy.** The destination is typically SMB or DrvFs, which
  stores no Unix modes, so the backup writes with `--no-perms`. The manifest records each mode
  that deviates from `0644` or `0755`, and restore applies it. Tests call `flatten_modes` before a
  restore, because a local destination keeps modes and would hide a replay that does nothing.
- **`linked_from` records sharing that was observed.** It compares inodes after the copy rather
  than trusting the flags passed. openrsync, which macOS ships as `/usr/bin/rsync`, lacks
  `--link-dest`, and an SMB destination can refuse `link()` without a word: rsync copies the file
  in full, names nothing and exits 0. So a backup's output never says what linking did until
  `link_verdict` has read the inodes.
- **One snapshot per run, named for the second it started, never pruned.** Two runs in one second
  share a name, and `merge_manifest` folds the second into the first. rsync never deletes, so a
  manifest naming only the second run would strand the first run's files.
- **A manifest key added later is read with a default.** Older snapshots lack it and are never
  rewritten.

## A backup narrows, and a restore must be told

`backup run` with no selection copies every source. `--tag` and `--source` narrow it, so there is
no `--all` to forget, and a narrowed run writes a snapshot holding only what it collected.
`restore` requires `--all`, `--source` or `--tag` and never infers one. On a terminal with none,
it opens fzf pickers instead. A selection that matches nothing exits 1.

## Every command safekeep prints runs as printed

- A hint starts with `safekeep_for(config_path)`. That is bare `safekeep` when there is one
  config, `-c NAME` among several, and `-c PATH` for a config outside the config directory.
  Prose names a config by `config_handle`, the word `-c` takes, never by its file name, which
  `-c` would read as a path relative to the working directory.
- A printed restore is built by `restore_command`. It quotes each word alone, so a path under the
  home keeps an unquoted tilde.
- A label comes before its command, so the command ends the line and copies whole.
- Tests run what was printed. `printed`, `printed_after` and `printed_line_containing` parse a
  command out of the output, and the test executes it.
  `test_every_help_example_names_commands_and_flags_safekeep_has` runs every help example with
  `-h` appended.
- Errors go to stderr, prefixed `safekeep:`, and a miss exits 1. A usage error is `ctx.fail` in
  `main.py`, exits 2, and names the command that gets past it. An fzf preview pane shows stderr
  and a failed exit, so a command that doubles as a pane needs no exception.
- An option on the wrong side of a command word is caught before click parses: a verb's option
  before the verb by `Namespace.parse_args`, and a root option after the command by
  `InWorkflowOrder.parse_args`. Each prints the line with the option moved.

## Paths follow XDG, so a test that moves HOME unsets them

Configs live in `$XDG_CONFIG_HOME/safekeep`, falling back to `~/.config/safekeep`. The rehearsal
directory the help and hints name is `$XDG_CACHE_HOME/safekeep/rehearsal`. A test or sandbox that
moves `HOME` must also unset `XDG_CONFIG_HOME` and `XDG_CACHE_HOME`, or it reads and writes the
real ones. The autouse fixture `config_and_cache_follow_home` does this for the suite.

## Tests run safekeep the way a user does

`run_safekeep` invokes `python -m safekeep` as a subprocess and strips color from both streams.
Typer forces color whenever `GITHUB_ACTIONS` is set, so an assertion on raw output passes locally
and fails in CI. The suite writes two backups well inside one second, so tests separate them with
`age_todays_snapshot` or `earlier_run_today` rather than sleeping.

## A config key that changes meaning fails the run

An unknown key warns. A key in `RETIRED_KEYS` gets its own message. A key in `RENAMED_KEYS` is
fatal, because ignoring it would shrink the backup without a word.

## Release

python-semantic-release cuts releases from conventional commits on `main`. `task test` runs the
suite, `task lint` runs ruff, mypy and bandit, and `task setup` makes a fresh worktree runnable.
