"""safekeep's command tree.

Typer owns the parsing, the help screens, `no_args_is_help` at every namespace, and exit 2 for a
usage error. Each command here is a thin wrapper: it resolves the config and hands the logic in
`safekeep` the flags it was given, as typed parameters or a request dataclass, so a renamed flag
fails the type check rather than a run.
"""

import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from typing import Any

import typer
from pyselfupdate import Config
from pyselfupdate import notify
from pyselfupdate.typercmd import run_update
from typer.core import TyperGroup

from safekeep import CONFIG_TEMPLATE
from safekeep import REHEARSAL_ROOT
from safekeep import BackupRequest
from safekeep import ConflictPolicy
from safekeep import RestoreRequest
from safekeep import do_backup
from safekeep import do_restore
from safekeep import edit_config
from safekeep import init_config
from safekeep import load_config
from safekeep import resolve_config
from safekeep import resolve_tag_index
from safekeep import safekeep_for
from safekeep import shell_path
from safekeep import show_config
from safekeep import show_files
from safekeep import show_snapshot_list
from safekeep import show_snapshot_record
from safekeep import show_snapshot_source_files
from safekeep import show_tag
from safekeep import show_tag_list
from safekeep import snapshot_choices
from safekeep import tool_version

# Notify-only: one check per 24h, one line to stderr, and `safekeep update` is the only thing that
# writes anything. The notice and the command share this config so the notice cannot name a release
# the command would not install.
UPDATE_CONFIG = Config(tool='safekeep', owner='datapointchris')

# The rehearsal directory as a command line carries it: under ~ where it can be, so it reads short.
REHEARSE_INTO = shell_path(str(REHEARSAL_ROOT))


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


class Namespace(TyperGroup):
    """A namespace takes only a verb, so an option typed before one belongs to a verb.

    The error then prints the command with each verb that takes the option in its place.
    """

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        if args and args[0].startswith('-') and args[0] not in ctx.help_option_names:
            flag = args[0].split('=', 1)[0]
            verbs = [name for name, command in self.commands.items() if any(flag in param.opts for param in command.params)]
            if verbs:
                typed = sys.argv[1:]
                before = typed[: len(typed) - len(args)] if typed[len(typed) - len(args) :] == args else ctx.command_path.split()[1:]
                retries = '\n'.join(shlex.join(['safekeep', *before, verb, *args]) for verb in verbs)
                ctx.fail(f'No such option: {flag}. It belongs to a verb, so it goes after one:\n{retries}')
        return super().parse_args(ctx, args)


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
        (f'safekeep restore --to {REHEARSE_INTO} --all', 'rehearse a restore'),
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


def config_file(ctx: typer.Context) -> Path:
    """The config the root options name, or the only one there is.

    The command line goes along so that, with several configs and no `-c`, the error can print it
    back with `-c` added.
    """
    return resolve_config(invocation(ctx).config, typed=sys.argv[1:])


def loaded(ctx: typer.Context):
    """(config path, config, warnings) for the config the root options name."""
    config_path = config_file(ctx)
    config, warnings = load_config(config_path)
    return config_path, config, warnings


def destination(config) -> Path:
    return Path(config['back_up_to']).expanduser()


def rehearsal(
    ctx: typer.Context,
    from_date: str | None,
    all_sources: bool,
    sources: list[str],
    tags: list[str],
    on_conflict: ConflictPolicy,
    skip_symlinked: bool,
    dry_run: bool,
) -> str:
    """The restore as it was typed, aimed at a scratch directory, for an error that has to name one."""
    root = invocation(ctx)
    head = ['safekeep']
    if root.config:
        head += ['-c', root.config]
    if root.no_input:
        head.append('--no-input')
    head.append('restore')
    tail = ['--from', from_date] if from_date else []
    selection = (['--all'] if all_sources else []) + [w for s in sources for w in ('--source', s)] + [w for t in tags for w in ('--tag', t)]
    tail += selection or ['--all']
    if on_conflict != ConflictPolicy.BACKUP:
        tail += ['--on-conflict', on_conflict.value]
    if skip_symlinked:
        tail.append('--skip-symlinked')
    if dry_run:
        tail.append('-n')
    # Joined apart from the rest because shlex.join would quote the tilde, and a quoted one never expands.
    return f'{shlex.join(head)} --to {REHEARSE_INTO} {shlex.join(tail)}'


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
        typer.Option(
            '--config',
            '-c',
            metavar='NAME|PATH',
            help='Which config to read: a name, or a .toml file. Needed only when there is more than one',
        ),
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
    cls=Namespace,
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
    request = BackupRequest(tag=tag or [], source=(source or []) + (group or []), label=label, dry_run=dry_run)
    do_backup(config, config_path, warnings, request)


# --- snapshots --------------------------------------------------------------------------------

snapshots_app = typer.Typer(
    cls=Namespace,
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


@snapshots_app.command(
    'list',
    epilog=examples(
        ('safekeep snapshots list', 'which backups exist, newest first'),
        ('safekeep snapshots list --json', 'the same, for a script'),
    ),
)
def snapshots_list(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """Every snapshot at the destination, newest first.

    A snapshot missing its manifest, the record of what it holds and the modes to restore, is listed
    and marked: safekeep cannot restore it.
    """
    config_path, config, _ = loaded(ctx)
    show_snapshot_list(destination(config), config_path, as_json)


# A miss exits 0 without --json because this is also the restore picker's preview pane, which has
# nowhere else to show one.
@snapshots_app.command(
    'show',
    epilog=examples(
        ('safekeep snapshots show 2026-08-13', "that day's last run, for a date from snapshots list"),
        ('safekeep snapshots show 2026-08-13 --source ~/.ssh', 'the files it holds for one source'),
    ),
)
def snapshots_show(
    ctx: typer.Context,
    date: Annotated[
        str | None,
        typer.Argument(
            metavar='DATE',
            help='Required. A snapshot as `safekeep snapshots list` names it, or a date for the last run that day',
            show_default=False,
        ),
    ] = None,
    source: Annotated[str | None, typer.Option('--source', metavar='PATH', help='The files that snapshot holds for one source')] = None,
    as_json: JsonOption = False,
) -> None:
    """One snapshot: its sources, sizes and tags.

    Without `--json` an absent snapshot or source prints the reason and exits 0. With `--json` it
    exits 1, so a script can tell an answer from a miss.
    """
    config_path, config, _ = loaded(ctx)
    if date is None:
        choices = snapshot_choices(destination(config), config_path)
        ctx.fail(f"Missing DATE: which snapshot to show. A date picks that day's last run.\n{choices}")
    if source:
        show_snapshot_source_files(destination(config), date, source, config_path, as_json)
    else:
        show_snapshot_record(destination(config), date, config_path, as_json)


# --- files ------------------------------------------------------------------------------------

files_app = typer.Typer(
    cls=Namespace,
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
    config_path, config, _ = loaded(ctx)
    show_files(config, config_path, missing=missing, from_date=from_date, as_json=as_json)


# --- tags -------------------------------------------------------------------------------------

tags_app = typer.Typer(
    cls=Namespace,
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


@tags_app.command(
    'list',
    epilog=examples(
        ('safekeep tags list', 'every tag, sized against the newest snapshot'),
        ('safekeep tags list --from 2026-07-01', 'sized against an older snapshot, for a date from snapshots list'),
    ),
)
def tags_list(ctx: typer.Context, from_date: SizeFromOption = None, as_json: JsonOption = False) -> None:
    """Every tag, the sources it covers, and what it would restore."""
    config_path, config, _ = loaded(ctx)
    show_tag_list(config, config_path, from_date=from_date, as_json=as_json)


@tags_app.command(
    'show',
    epilog=examples(
        ('safekeep tags show secrets', 'what restore --tag secrets would bring back, for a tag from tags list'),
    ),
)
def tags_show(
    ctx: typer.Context,
    name: Annotated[
        str | None, typer.Argument(metavar='NAME', help='Required. A tag as `safekeep tags list` names it', show_default=False)
    ] = None,
    from_date: SizeFromOption = None,
    as_json: JsonOption = False,
) -> None:
    """One tag, source by source, and the restore that brings it back."""
    config_path, config, _ = loaded(ctx)
    if name is None:
        index, _, _ = resolve_tag_index(config, config_path, from_date)
        tags = ', '.join(sorted(index)) if index else f'none yet, so tag the sources with {safekeep_for(config_path)} config edit'
        ctx.fail(f'Missing NAME: which tag to show. Tags: {tags}')
    show_tag(config, config_path, name=name, from_date=from_date, as_json=as_json)


# --- restore ----------------------------------------------------------------------------------


@app.command(
    'restore',
    no_args_is_help=True,
    rich_help_panel='Restore',
    epilog=examples(
        (f'safekeep restore --to {REHEARSE_INTO} --all', 'rehearse first, always'),
        ('safekeep restore --to / --tag secrets', 'restore one tag for real'),
        ('safekeep restore --to / --from 2026-07-01 --all', 'restore an older snapshot'),
        ('safekeep restore --to / --from 2026-07-01 --source ~/.ssh/config', 'bring back one file'),
    ),
)
def restore(
    ctx: typer.Context,
    to: Annotated[
        str | None,
        typer.Option(
            '--to',
            metavar='PATH',
            help=f'Required. Root to restore into: `/` for a real restore, or a scratch directory such as `{REHEARSE_INTO}` to rehearse',
            show_default=False,
        ),
    ] = None,
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
        ConflictPolicy,
        typer.Option('--on-conflict', metavar='POLICY', help=f'What to do with a file already at the target: {", ".join(ConflictPolicy)}'),
    ] = ConflictPolicy.BACKUP,
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
    sources = (source or []) + (group or [])
    # Resolved before the --to check: with several configs and no -c, a rehearsal printed first would fail as printed.
    config_path, config, _ = loaded(ctx)
    if to is None:
        rehearse = rehearsal(ctx, from_date, all_sources, sources, tag or [], on_conflict, skip_symlinked, dry_run)
        ctx.fail(f'Missing --to: the root to restore into. --to / puts files back where they were.\nRehearse this one first: {rehearse}')
    request = RestoreRequest(
        # Expanded here because no shell expands a quoted ~, or one after --to= in zsh.
        to=str(Path(to).expanduser()),
        from_date=from_date,
        all=all_sources,
        source=sources,
        tag=tag or [],
        on_conflict=on_conflict,
        skip_symlinked=skip_symlinked,
        dry_run=dry_run,
        no_input=invocation(ctx).no_input,
    )
    do_restore(config, config_path, request)


# --- config -----------------------------------------------------------------------------------

config_app = typer.Typer(
    cls=Namespace,
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


@config_app.command(
    'show',
    epilog=examples(
        ('safekeep config show', 'the config in use, which file it is, and any warnings about it'),
        ('safekeep -c work config show', 'another config, by name'),
    ),
)
def config_show(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """Display the resolved config and exit."""
    config_path, config, warnings = loaded(ctx)
    show_config(config_path, config, warnings, as_json)


@config_app.command('edit', epilog=examples(('safekeep config edit', 'change the config, then see what the edit changed')))
def config_edit(ctx: typer.Context) -> None:
    """Open the config in $VISUAL or $EDITOR, then check it."""
    # Resolved but not loaded: a config that fails to load is the main reason to open one, and
    # load_config exits before the editor could fix it.
    edit_config(config_file(ctx))


@config_app.command(
    'init',
    epilog=examples(
        ('safekeep config init', 'a starter config named default'),
        ('safekeep config init work', 'a second config; every command then names one with -c'),
    ),
)
def config_init(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(metavar='NAME|PATH', help='A name, or a .toml file to write. `-c` overrides it')] = 'default',
) -> None:
    """Write a starter config."""
    init_config(invocation(ctx).config or name)


@config_app.command('example', epilog=examples(('safekeep config example', 'every key, explained, without writing a file')))
def config_example() -> None:
    """Print the annotated example without writing it."""
    print(CONFIG_TEMPLATE, end='')


# --- update -----------------------------------------------------------------------------------


@app.command(
    'update',
    rich_help_panel='Manage',
    epilog=examples(
        ('safekeep update --check', 'whether a newer release exists, and what changed in it'),
        ('safekeep update', 'install it'),
    ),
)
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
