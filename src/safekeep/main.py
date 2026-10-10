"""safekeep's command tree.

Typer owns the parsing, the help screens, `no_args_is_help` at every namespace, and exit 2 for a
usage error. Each command here is a thin wrapper: it resolves the config and hands the logic in
`safekeep` the flags it was given, so that logic stays testable without a runner.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated

import typer
from pyselfupdate import Config
from pyselfupdate import notify
from pyselfupdate.typercmd import run_update
from typer.core import TyperGroup

from safekeep import CONFIG_TEMPLATE
from safekeep import do_backup
from safekeep import do_restore
from safekeep import edit_config
from safekeep import init_config
from safekeep import load_config
from safekeep import resolve_config
from safekeep import show_config
from safekeep import show_files
from safekeep import show_snapshot_list
from safekeep import show_snapshot_record
from safekeep import show_snapshot_source_files
from safekeep import show_tag
from safekeep import show_tag_list
from safekeep import tool_version

# Notify-only: one check per 24h, one line to stderr, and `safekeep update` is the only thing that
# writes anything. The notice and the command share this config so the notice cannot name a release
# the command would not install.
UPDATE_CONFIG = Config(tool='safekeep', owner='datapointchris')


def examples(*rows: tuple[str, str]) -> str:
    """An epilog of runnable invocations, each with what it is for."""
    return '**Examples**\n\n' + '\n'.join(f'- `{command}` — {purpose}' for command, purpose in rows)


class InWorkflowOrder(TyperGroup):
    """The root's commands in the order a backup is used: write, read, restore, then manage.

    Typer lists a group's commands ahead of its sub-apps, which would open the screen on restore.
    """

    ORDER = ('backup', 'snapshots', 'files', 'tags', 'restore', 'config', 'update')

    def list_commands(self, ctx: object) -> list[str]:
        return sorted(self.commands, key=lambda name: self.ORDER.index(name) if name in self.ORDER else len(self.ORDER))


app = typer.Typer(
    name='safekeep',
    cls=InWorkflowOrder,
    no_args_is_help=True,
    rich_markup_mode='markdown',
    context_settings={'help_option_names': ['-h', '--help']},
    help="""Timestamped snapshots of the files no package manager will put back.

    The verb comes last, so a backup → snapshots → restore loop over one destination changes only
    the final word. Nothing acts until a verb selects it: every partial command prints the screen
    that completes it.

    Configs live in `~/.config/safekeep/<name>.toml`, one per backup destination, and
    `safekeep config example` prints an annotated one explaining every key. `-c` and `--no-input`
    go before the command: `safekeep -c work backup run`.

    Selection is always explicit, so a restore never guesses at `--all`. Rehearse into a scratch
    directory before restoring over anything real. A snapshot carries the tags its config had that
    day, so `safekeep tags list` is what says whether `--tag` selects anything in it.
    """,
    epilog=examples(
        ('safekeep config init', 'write ~/.config/safekeep/default.toml'),
        ('safekeep backup run -n', 'see what a backup would copy'),
        ('safekeep snapshots list', 'what is on the destination already'),
        ('safekeep snapshots show 2026-08-13', 'what one snapshot holds'),
        ('safekeep files list --missing', 'what older snapshots hold that this machine lacks'),
        ('safekeep tags show secrets', 'what that tag would bring back'),
        ('safekeep restore --to /tmp/restore-test --all', 'rehearse a restore'),
        ('safekeep restore --to / --tag secrets', 'restore one tag for real'),
    ),
)


@dataclass
class Invocation:
    """The root options, which every command below reads."""

    config: str | None
    no_input: bool


def invocation(ctx: typer.Context) -> Invocation:
    return ctx.find_root().obj


def loaded(ctx: typer.Context):
    """(config path, config, warnings) for the config the root options name."""
    config_path = resolve_config(invocation(ctx).config)
    config, warnings = load_config(config_path)
    return config_path, config, warnings


def destination(config) -> Path:
    return Path(config['back_up_to']).expanduser()


def flags(ctx: typer.Context, **given) -> SimpleNamespace:
    """A command's flags in the shape the logic in `safekeep` reads them."""
    return SimpleNamespace(no_input=invocation(ctx).no_input, **given)


def version_callback(asked: bool) -> None:
    if not asked:
        return
    print(f'safekeep {tool_version()}')
    raise typer.Exit()


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    config: Annotated[
        str | None,
        typer.Option('--config', '-c', metavar='NAME|PATH', help='Config to use, by name or path (default: auto-detect)'),
    ] = None,
    no_input: Annotated[bool, typer.Option('--no-input', help='Never prompt; fail naming the flag that would have answered')] = False,
    version: Annotated[
        bool | None,
        typer.Option('--version', '-V', callback=version_callback, is_eager=True, help='Print the running version'),
    ] = None,
) -> None:
    ctx.obj = Invocation(config=config, no_input=no_input)
    if ctx.invoked_subcommand is None:
        # no_args_is_help misses `safekeep -c work`, because a root option makes argv non-empty.
        typer.echo(ctx.get_help())
        raise typer.Exit(2)
    # `update` runs its own check, so a notice too would ask the releases API twice for one command.
    if ctx.invoked_subcommand != 'update':
        notify(UPDATE_CONFIG)


DryRunOption = Annotated[bool, typer.Option('--dry-run', '-n', help='Show what would be done, change nothing')]
JsonOption = Annotated[bool, typer.Option('--json', help='Output as JSON to stdout')]
# --group is accepted for --source and kept off the screen: it is one word in an older shell history,
# and accepting it cannot make a run cover less than it was asked to.
GroupAlias = Annotated[list[str] | None, typer.Option('--group', metavar='PATH', hidden=True)]


# --- backup -----------------------------------------------------------------------------------

backup_app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode='markdown',
    help="""Copy the configured paths into a new snapshot.

    `safekeep backup run` copies everything the config lists. `--tag` and `--source` narrow it, and
    a narrowed run records only what it collected, so it writes a partial snapshot rather than
    topping up a fuller one.
    """,
    epilog=examples(
        ('safekeep backup run', 'everything the config lists'),
        ('safekeep backup run -n', 'what a backup would copy, before it copies it'),
        ("safekeep backup run --label 'before the wsl move'", 'say why, for whoever restores it'),
    ),
)
app.add_typer(backup_app, name='backup', rich_help_panel='Back up')


@backup_app.command(
    'run',
    epilog=examples(
        ('safekeep backup run --tag secrets', 'just the secrets, before doing something risky'),
        ('safekeep backup run --source ~/.ssh', 'one path, without walking the rest'),
        ("safekeep backup run --label 'before the wsl move'", 'say why, for whoever restores it'),
    ),
)
def backup_run(
    ctx: typer.Context,
    tag: Annotated[
        list[str] | None,
        typer.Option('--tag', metavar='NAME', help='Only entries carrying NAME (repeatable)', rich_help_panel='Selection'),
    ] = None,
    source: Annotated[
        list[str] | None,
        typer.Option('--source', metavar='PATH', help='Only entries whose path contains PATH (repeatable)', rich_help_panel='Selection'),
    ] = None,
    group: GroupAlias = None,
    label: Annotated[str | None, typer.Option('--label', metavar='NOTE', help='Why this backup was taken, kept in the snapshot')] = None,
    dry_run: DryRunOption = False,
) -> None:
    """Copy the configured paths into a new snapshot.

    Everything the config lists, unless `--tag` or `--source` narrows it. A narrowed run records only
    what it collected, so it writes a partial snapshot beside the full one rather than topping it up.
    `safekeep tags list` says which names there are to narrow by.

    A label is free text the tool never reads: `snapshots list`, `snapshots show` and the restore
    picker display it. A date says when a snapshot was taken and nothing about why, which is what a
    label answers. A snapshot is one run, so a later backup cannot overwrite its label.
    """
    config_path, config, warnings = loaded(ctx)
    # None rather than '' when --label is absent: the manifest writer records the key only when it was typed.
    given = flags(ctx, tag=tag or [], source=(source or []) + (group or []), label=label, dry_run=dry_run)
    do_backup(config, config_path, warnings, given)


# --- snapshots --------------------------------------------------------------------------------

snapshots_app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode='markdown',
    help='What is at the destination, and what each snapshot holds.',
    epilog=examples(
        ('safekeep snapshots list', 'what is on the destination already'),
        ('safekeep snapshots show 2026-08-13', 'what that day captured'),
        ('safekeep snapshots show 2026-08-13 --source ~/.ssh', 'the files it holds for one source'),
    ),
)
app.add_typer(snapshots_app, name='snapshots', rich_help_panel='Read')


@snapshots_app.command('list')
def snapshots_list(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """Every snapshot at the destination, newest first.

    A snapshot with no manifest is listed and says so. safekeep cannot restore it, and the reason
    belongs on the row rather than in a later failure.
    """
    _, config, _ = loaded(ctx)
    show_snapshot_list(destination(config), as_json)


@snapshots_app.command('show')
def snapshots_show(
    ctx: typer.Context,
    date: Annotated[
        str,
        typer.Argument(
            metavar='DATE', help='A snapshot as `safekeep snapshots list` names it, or a date for the last run that day', show_default=False
        ),
    ],
    source: Annotated[str | None, typer.Option('--source', metavar='PATH', help='The files that snapshot holds for one source')] = None,
    as_json: JsonOption = False,
) -> None:
    """One snapshot: its sources, sizes and tags.

    Without `--json` an absent snapshot or source prints the reason and succeeds, because this is
    also the restore picker's preview pane. With `--json` it exits 1, so a caller can tell an answer
    from a miss.
    """
    _, config, _ = loaded(ctx)
    if source:
        show_snapshot_source_files(destination(config), date, source, as_json)
    else:
        show_snapshot_record(destination(config), date, as_json)


# --- files ------------------------------------------------------------------------------------

files_app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode='markdown',
    help='Every file the snapshots hold, one line each.',
    epilog=examples(
        ('safekeep files list --missing', 'what older snapshots hold that this machine lacks'),
        ('safekeep files list --from 2026-08-04', 'everything one snapshot holds, flat'),
    ),
)
app.add_typer(files_app, name='files', rich_help_panel='Read')


@files_app.command(
    'list',
    epilog=examples(
        ('safekeep files list --missing', 'what older snapshots hold that this machine lacks'),
        ('safekeep files list --from 2026-08-04', 'everything one snapshot holds, flat'),
        ('safekeep files list --missing --json', "the same, with each stored copy's path"),
        ('safekeep restore --to / --from <snapshot> --source ~/path/to/file', 'bring one back'),
    ),
)
def files_list(
    ctx: typer.Context,
    missing: Annotated[bool, typer.Option('--missing', help='Only the files that are not on this machine')] = False,
    from_date: Annotated[str | None, typer.Option('--from', metavar='DATE', help='Read that one snapshot instead of all of them')] = None,
    as_json: JsonOption = False,
) -> None:
    """Every file across every snapshot, from the newest holding it.

    Each file is listed once, under the newest snapshot that holds it, which is the copy a restore
    would bring back. A file six directories deep is still one line. A file an older machine had and
    this one lacks is only in the older snapshots, because each backup copies only what the machine
    running it has.

    To bring one back, name the snapshot it is listed under and the file itself as the source. It is
    backed up from then on only if a config entry covers its path. After the next backup run, its
    line here names the newest snapshot if one does.
    """
    _, config, _ = loaded(ctx)
    show_files(config, flags(ctx, missing=missing, from_date=from_date, as_json=as_json))


# --- tags -------------------------------------------------------------------------------------

tags_app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode='markdown',
    help="""The tags a restore can select on, and what each would bring back.

    A tag lives in two places and reading either alone misleads: the config says which entries carry
    it, and each snapshot carries a copy of what the config said that day. `--tag` selects on the
    snapshot, so tagging an entry today does nothing for the snapshots that already exist. Both verbs
    mark the difference.
    """,
    epilog=examples(
        ('safekeep tags list', 'every tag and what it would restore'),
        ('safekeep tags show secrets', 'what that tag would bring back'),
        ('safekeep tags list --from 2026-07-01', 'size against an older snapshot'),
    ),
)
app.add_typer(tags_app, name='tags', rich_help_panel='Read')
SizeFromOption = Annotated[str | None, typer.Option('--from', metavar='DATE', help='Size against that snapshot instead of the newest')]


@tags_app.command('list')
def tags_list(ctx: typer.Context, from_date: SizeFromOption = None, as_json: JsonOption = False) -> None:
    """Every tag, the sources it covers, and what it would restore."""
    config_path, config, _ = loaded(ctx)
    show_tag_list(config, config_path, flags(ctx, from_date=from_date, as_json=as_json))


@tags_app.command('show')
def tags_show(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(metavar='NAME', help='A tag as `safekeep tags list` names it', show_default=False)],
    from_date: SizeFromOption = None,
    as_json: JsonOption = False,
) -> None:
    """One tag, source by source, and the restore that brings it back."""
    config_path, config, _ = loaded(ctx)
    show_tag(config, config_path, flags(ctx, name=name, from_date=from_date, as_json=as_json))


# --- restore ----------------------------------------------------------------------------------


class OnConflict(StrEnum):
    BACKUP = 'backup'
    SKIP = 'skip'
    OVERWRITE = 'overwrite'
    NEWER = 'newer'
    ASK = 'ask'


@app.command(
    'restore',
    no_args_is_help=True,
    rich_help_panel='Restore',
    epilog=examples(
        ('safekeep restore --to /tmp/restore-test --all', 'rehearse first, always'),
        ('safekeep restore --to / --tag secrets', 'restore one tag for real'),
        ('safekeep restore --to / --from 2026-07-01 --all', 'restore an older snapshot'),
        ('safekeep restore --to / --from 2026-07-01 --source ~/.ssh/config', 'bring back one file'),
    ),
)
def restore(
    ctx: typer.Context,
    to: Annotated[
        str,
        typer.Option(
            '--to',
            metavar='PATH',
            help='Root to restore into: `/` for a real restore, or a scratch directory such as `/tmp/restore-test` to rehearse',
            show_default=False,
        ),
    ],
    from_date: Annotated[
        str | None, typer.Option('--from', metavar='DATE', help='Snapshot to restore from (default: pick, else newest)')
    ] = None,
    all_sources: Annotated[bool, typer.Option('--all', help='Every source in the snapshot', rich_help_panel='Selection')] = False,
    source: Annotated[
        list[str] | None,
        typer.Option(
            '--source',
            metavar='PATH',
            help='Sources whose path contains PATH, or one file or directory inside one (repeatable)',
            rich_help_panel='Selection',
        ),
    ] = None,
    group: GroupAlias = None,
    tag: Annotated[
        list[str] | None, typer.Option('--tag', metavar='NAME', help='Sources carrying NAME (repeatable)', rich_help_panel='Selection')
    ] = None,
    on_conflict: Annotated[
        OnConflict, typer.Option('--on-conflict', help='What to do with a file already at the target')
    ] = OnConflict.BACKUP,
    skip_symlinked: Annotated[
        bool, typer.Option('--skip-symlinked', help='Skip paths that were symlinks when backed up, or sat under one')
    ] = False,
    dry_run: DryRunOption = False,
) -> None:
    """Restore sources from a snapshot.

    Selection is required and never inferred, so pass `--all`, `--source` or `--tag`. A source is one
    config entry: a path, or one repo's untracked and ignored files. A full path to something inside
    a source restores that alone, and `safekeep files list` prints those paths, one line per file. A
    tag selects on the snapshot, which carries the tags its config had that day, and
    `safekeep tags show <name>` says whether one selects anything.

    Every file is named as it is written, marked `+` for new and `~` for replaced. `backup` keeps the
    file it replaced beside it as `<name>.pre-restore`. `ask` names each existing file and waits for a
    decision, `[y]es [N]o [a]ll [k]eep all [q]uit`, and keeps no copies, since you were asked.
    """
    config_path, config, _ = loaded(ctx)
    given = flags(
        ctx,
        to=to,
        from_date=from_date,
        all=all_sources,
        source=(source or []) + (group or []),
        tag=tag or [],
        on_conflict=on_conflict.value,
        skip_symlinked=skip_symlinked,
        dry_run=dry_run,
    )
    do_restore(config, config_path, given)


# --- config -----------------------------------------------------------------------------------

config_app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode='markdown',
    help="""Inspect and create config files.

    Each lives at `~/.config/safekeep/<name>.toml`. The annotated example is the key reference: it
    explains every key inline.
    """,
    epilog=examples(
        ('safekeep config init', 'write ~/.config/safekeep/default.toml'),
        ('safekeep config init work', 'a second destination, used with -c work'),
        ('safekeep config show', 'what the config resolves to'),
    ),
)
app.add_typer(config_app, name='config', rich_help_panel='Manage')


@config_app.command('show')
def config_show(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """Display the resolved config and exit."""
    config_path, config, warnings = loaded(ctx)
    show_config(config_path, config, warnings, as_json)


@config_app.command('edit')
def config_edit(ctx: typer.Context) -> None:
    """Open the config in $VISUAL or $EDITOR, then check it."""
    # Resolved but not loaded: a config that fails to load is the main reason to open one, and
    # load_config exits before the editor could fix it.
    edit_config(resolve_config(invocation(ctx).config))


@config_app.command('init')
def config_init(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(metavar='NAME', help='Name of the config to write; `-c` overrides it')] = 'default',
) -> None:
    """Write a starter config."""
    init_config(invocation(ctx).config or name)


@config_app.command('example')
def config_example() -> None:
    """Print the annotated example without writing it."""
    print(CONFIG_TEMPLATE, end='')


# --- update -----------------------------------------------------------------------------------


@app.command('update', rich_help_panel='Manage')
def update(
    check_only: Annotated[bool, typer.Option('--check', help='Report whether an update is available without installing it')] = False,
    skip_changelog: Annotated[bool, typer.Option('--no-changelog', help='Do not list the commits between versions')] = False,
) -> None:
    """Install the newest release."""
    run_update(UPDATE_CONFIG, check_only=check_only, skip_changelog=skip_changelog)


def main() -> None:
    # Named outright: under `python -m safekeep`, which the tests and the picker's preview panes run,
    # the name would otherwise come from argv and every usage line would read `python -m safekeep`.
    app(prog_name='safekeep')
