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
from safekeep import restore_command
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

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        """A root option typed after the command fails with the command printed with it moved before."""
        valued = {opt for param in self.params if not getattr(param, 'is_flag', True) for opt in param.opts}
        movable = {opt for param in self.params if param.name in ('config', 'no_input') for opt in param.opts}
        at = 0
        while at < len(args) and args[at].startswith('-'):
            at += 2 if args[at] in valued else 1
        if at < len(args) and args[at] in self.commands:
            rest = args[at + 1 :]
            for i, word in enumerate(rest):
                if word == '--':
                    break
                flag = word.split('=', 1)[0]
                if flag in movable:
                    width = 2 if word in valued else 1
                    retry = shlex.join(['safekeep', *args[:at], *rest[i : i + width], args[at], *rest[:i], *rest[i + width :]])
                    ctx.fail(f'No such option: {flag} after the command. It belongs to safekeep itself, so it goes before:\n{retry}')
        return super().parse_args(ctx, args)


class Namespace(TyperGroup):
    """A namespace takes only a verb, so an option typed before one belongs to a verb.

    The error then prints the command with each verb that takes the option in its place.
    """

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        if args and args[0].startswith('-') and args[0] not in ctx.help_option_names:
            flag = args[0].split('=', 1)[0]
            verbs = [name for name, command in self.commands.items() if any(flag in param.opts for param in command.params)]
            typed = sys.argv[1:]
            before = typed[: len(typed) - len(args)] if typed[len(typed) - len(args) :] == args else ctx.command_path.split()[1:]
            # A verb typed after the options is the one they were meant for, so they move behind it.
            at = next((i for i, word in enumerate(args) if word in self.commands), None)
            if at is not None and args[at] in verbs:
                retry = shlex.join(['safekeep', *before, args[at], *args[at + 1 :], *args[:at]])
                ctx.fail(f'No such option: {flag}. It belongs to {args[at]}, so it goes after it:\n{retry}')
            if at is None and verbs:
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

    A config names a destination, `back_up_to`, and the sources to copy there: paths, and the
    untracked and ignored files of git repos. A source can carry tags, names a restore selects by.
    Each `backup run` copies the sources into a new snapshot, a directory named for the second it
    started. The snapshot's manifest records what it holds and the file modes the destination
    cannot keep, so it restores without the config that wrote it.

    To start, `safekeep config init` writes a config, and `safekeep backup run -n` shows what it
    would copy. To get files back, rehearse the restore into a scratch directory, then run it with
    `--to /`. To find what an older machine had and this one lacks, `safekeep files list --missing`.
    On a new machine with only the drive, a config holding just `back_up_to` reads every snapshot.

    `-c` names the config when there are several, and goes before the command.
    """,
    epilog=examples(
        ('safekeep config init', 'a first config, to set back_up_to and the sources in'),
        ('safekeep backup run -n', 'what a backup would copy, before it copies anything'),
        ('safekeep backup run', 'take a snapshot'),
        ('safekeep snapshots list', 'the snapshots at the destination, newest first'),
        ('safekeep files list --missing', 'what older snapshots hold that this machine lacks'),
        (f'safekeep restore --to {REHEARSE_INTO} --all', 'rehearse restoring the newest snapshot'),
        ('safekeep restore --to / --tag secrets', 'restore the sources tagged secrets, for real'),
        ('safekeep -c work snapshots list', 'the snapshots of the config named work'),
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
    no_input: Annotated[
        bool, typer.Option('--no-input', help='Never prompt. A question this run would ask fails instead, naming the flag that answers it')
    ] = False,
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
    help="""Copy the config's sources into a new snapshot.

    `safekeep backup run` copies every source the config lists. `--tag` and `--source` narrow it,
    and a narrowed run records only what it collected, so it writes a partial snapshot rather than
    topping up a fuller one.
    """,
    epilog=examples(
        ('safekeep backup run', 'every source the config lists'),
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
        typer.Option('--tag', metavar='NAME', help='Only the sources tagged NAME in the config (repeatable)', rich_help_panel='Selection'),
    ] = None,
    source: Annotated[
        list[str] | None,
        typer.Option(
            '--source',
            metavar='PATH',
            help='Only the source at PATH, or else every source whose path contains PATH (repeatable)',
            rich_help_panel='Selection',
        ),
    ] = None,
    group: GroupAlias = None,
    label: Annotated[str | None, typer.Option('--label', metavar='NOTE', help='Why this backup was taken, kept in the snapshot')] = None,
    dry_run: DryRunOption = False,
) -> None:
    """Copy the config's sources into a new snapshot.

    A source is one entry in the config: a path, or one git repo's untracked and ignored files.
    Every source is copied unless `--tag` or `--source` narrows the run. A narrowed run records only
    what it collected, so it writes a partial snapshot beside the full one rather than topping it up,
    and `snapshots list` marks it `narrowed`. A selection every source matches leaves nothing out,
    so its snapshot is a full one. `safekeep tags list` says which tags there are to narrow by.

    Each file copied is named as it is copied. A file unchanged since the previous snapshot is not
    named: it becomes a hard link into that snapshot, costing no space, or a full copy where the
    destination cannot hard-link. The run's last line says which of the two happened.

    A label is free text that `snapshots list` and `snapshots show` print beside the snapshot, and
    that a restore shows when it lists snapshots to choose from. A date says when a snapshot was
    taken and nothing about why, which is what a label answers.
    """
    config_path, config, warnings = loaded(ctx)
    request = BackupRequest(tag=tag or [], source=(source or []) + (group or []), label=label, dry_run=dry_run)
    do_backup(config, config_path, warnings, request)


# --- snapshots --------------------------------------------------------------------------------

snapshots_app = typer.Typer(
    cls=Namespace,
    no_args_is_help=True,
    rich_markup_mode='markdown',
    help="""What is at the destination, and what each snapshot holds.

    A snapshot is one backup run: a directory at the destination named for the second the run
    started, holding the copied files and a manifest that records what they are.
    """,
    epilog=examples(
        ('safekeep snapshots list', 'the snapshots at the destination, newest first'),
        ('safekeep snapshots show 2026-08-13', "that day's last run, for a date snapshots list shows"),
        ('safekeep snapshots show 2026-08-13 --source ~/.ssh', 'the files that run holds for one source'),
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

    Each row is one backup run: its name, size, files, sources, the machine it ran on and its label.
    A run narrowed by `--tag` or `--source` is marked `narrowed`, since it holds only the sources it
    selected. A snapshot missing its manifest, the record of what it holds and the modes to restore,
    is listed and marked, because safekeep cannot restore it.
    """
    config_path, config, _ = loaded(ctx)
    show_snapshot_list(destination(config), config_path, as_json)


@snapshots_app.command(
    'show',
    epilog=examples(
        ('safekeep snapshots show 2026-08-13', "that day's last run, for a date snapshots list shows"),
        ('safekeep snapshots show 2026-08-13T17-04-32', 'one run, by its full name'),
        ('safekeep snapshots show 2026-08-13 --source ~/.ssh', 'the files that run holds for one source'),
    ),
)
def snapshots_show(
    ctx: typer.Context,
    date: Annotated[
        str | None,
        typer.Argument(
            metavar='SNAPSHOT',
            help="Required. A snapshot as `safekeep snapshots list` names it, or a date for that day's last run",
            show_default=False,
        ),
    ] = None,
    source: Annotated[str | None, typer.Option('--source', metavar='PATH', help='The files the snapshot holds for one source')] = None,
    as_json: JsonOption = False,
) -> None:
    """One snapshot: the machine and config that wrote it, and its sources with their sizes and tags.

    An absent snapshot or source prints the reason on stderr and exits 1.
    """
    config_path, config, _ = loaded(ctx)
    if date is None:
        choices = snapshot_choices(destination(config), config_path)
        ctx.fail(f"Missing SNAPSHOT: which snapshot to show. A date picks that day's last run.\n{choices}")
    if source:
        show_snapshot_source_files(destination(config), date, source, config_path, as_json)
    else:
        show_snapshot_record(destination(config), date, config_path, as_json)


# --- files ------------------------------------------------------------------------------------

files_app = typer.Typer(
    cls=Namespace,
    no_args_is_help=True,
    rich_markup_mode='markdown',
    help='Every file the snapshots hold, one line each, and which of them this machine lacks.',
    epilog=examples(
        ('safekeep files list --missing', 'what older snapshots hold that this machine lacks'),
        ('safekeep files list --from 2026-08-04', 'everything one snapshot holds, flat, for a date snapshots list shows'),
    ),
)
app.add_typer(files_app, name='files', rich_help_panel='Read')


@files_app.command(
    'list',
    epilog=examples(
        ('safekeep files list --missing', 'what older snapshots hold that this machine lacks'),
        ('safekeep files list --from 2026-08-04', 'everything one snapshot holds, flat, for a date snapshots list shows'),
        ('safekeep files list --missing --json', "the same, with each stored copy's path"),
        ('safekeep restore --to / --from 2026-08-04 --source ~/.gitconfig', 'bring one back from the snapshot it is listed under'),
    ),
)
def files_list(
    ctx: typer.Context,
    missing: Annotated[bool, typer.Option('--missing', help='Only the files that are not on this machine')] = False,
    from_date: Annotated[
        str | None, typer.Option('--from', metavar='SNAPSHOT', help='Read that one snapshot instead of all of them')
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Every file across every snapshot, from the newest holding it.

    Each file is listed once, under the newest snapshot that holds it, which is the copy a restore
    would bring back. A file six directories deep is still one line. A file an older machine had and
    this one lacks is only in the older snapshots, because each backup copies only what the machine
    running it has.

    To bring one back, restore it with `--from` the snapshot it is listed under and `--source` the
    file itself. A file brought back is backed up again only if a source in the config covers its
    path. Once one does, the next backup run lists it under the newest snapshot.
    """
    config_path, config, _ = loaded(ctx)
    show_files(config, config_path, missing=missing, from_date=from_date, as_json=as_json)


# --- tags -------------------------------------------------------------------------------------

tags_app = typer.Typer(
    cls=Namespace,
    no_args_is_help=True,
    rich_markup_mode='markdown',
    help="""The tags a restore can select by, and what each would bring back.

    A tag is a name given to sources in the config, such as `secrets`, and `restore --tag secrets`
    restores every source carrying it. Each snapshot keeps a copy of the tags its config had that day,
    and a restore selects on that copy, so tagging a source today does nothing for the snapshots that
    already exist. Both verbs read the config and one snapshot together, and mark where they differ.
    """,
    epilog=examples(
        ('safekeep tags list', 'every tag and what a restore by it would bring back'),
        ('safekeep tags show secrets', 'the sources one tag covers, and the restore that brings them back'),
        ('safekeep tags list --from 2026-07-01', 'the same, read from an older snapshot'),
    ),
)
app.add_typer(tags_app, name='tags', rich_help_panel='Read')
TagsFromOption = Annotated[
    str | None, typer.Option('--from', metavar='SNAPSHOT', help="Read that snapshot's tags and sizes instead of the newest's")
]


@tags_app.command(
    'list',
    epilog=examples(
        ('safekeep tags list', 'every tag, read from the newest snapshot'),
        ('safekeep tags list --from 2026-07-01', 'read from an older snapshot, for a date snapshots list shows'),
    ),
)
def tags_list(ctx: typer.Context, from_date: TagsFromOption = None, as_json: JsonOption = False) -> None:
    """Every tag, the sources it covers, and what a restore by it would bring back.

    A tag is a name given to sources in the config. A restore by tag selects on the tags a snapshot
    recorded, so each tag is read from one snapshot, the newest unless `--from` names another. Its
    size is what a restore by that tag would bring back from it. A tag the snapshot lacks reads
    "restores nothing from this snapshot".
    """
    config_path, config, _ = loaded(ctx)
    show_tag_list(config, config_path, from_date=from_date, as_json=as_json)


@tags_app.command(
    'show',
    epilog=examples(
        ('safekeep tags show secrets', 'what restore --tag secrets would bring back, for a tag tags list shows'),
        ('safekeep tags show secrets --from 2026-07-01', 'the same, from an older snapshot'),
    ),
)
def tags_show(
    ctx: typer.Context,
    name: Annotated[
        str | None, typer.Argument(metavar='NAME', help='Required. A tag as `safekeep tags list` names it', show_default=False)
    ] = None,
    from_date: TagsFromOption = None,
    as_json: JsonOption = False,
) -> None:
    """One tag, source by source, and the restore that brings it back.

    A tag is a name given to sources in the config. Each source shows what the snapshot holds for it,
    or why a restore by this tag would skip it. The restore printed beneath runs as printed, into a
    scratch directory; where the snapshot holds the files but not the tag, it restores them by path.
    """
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
        (f'safekeep restore --to {REHEARSE_INTO} --all', 'rehearse first: the newest snapshot, into a scratch directory'),
        ('safekeep restore --to / --tag secrets', 'restore the sources tagged secrets, for real'),
        ('safekeep restore --to / --from 2026-07-01 --all', 'restore an older snapshot, for a date snapshots list shows'),
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
        str | None,
        typer.Option(
            '--from',
            metavar='SNAPSHOT',
            help="The snapshot to restore from, as `snapshots list` names it, or a date for that day's last run",
        ),
    ] = None,
    all_sources: Annotated[bool, typer.Option('--all', help='Every source in the snapshot', rich_help_panel='Selection')] = False,
    source: Annotated[
        list[str] | None,
        typer.Option(
            '--source',
            metavar='PATH',
            help='The source at PATH, or else every source whose path contains PATH, or one file or directory inside one (repeatable)',
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

    Selection is required and never inferred: `--all`, `--source` or `--tag`. A source is one entry
    in the config: a path, or one git repo's untracked and ignored files. `--source` also takes the
    full path of a file or directory inside a source, which `safekeep files list` prints. A tag
    selects on the tags the snapshot recorded, and `safekeep tags show NAME` says what it selects.

    Without `--from` the restore reads the newest snapshot. Where a narrowed backup took that one,
    `--all` restores what it holds, then prints a restore for the rest from the newest snapshot
    holding each source. On a terminal with nothing selected, it lists the snapshots to choose from
    instead, then the sources in the one chosen. Files from under the home that took the snapshot
    land under this machine's home.

    Every file is named as it is written, `+` for new and `~` for replaced. `--on-conflict` decides
    what happens to a file already at the target. `backup` replaces it and keeps the old one beside
    it, with `.pre-restore` added to its name. `overwrite` replaces it and keeps nothing. `skip`
    leaves it. `newer` replaces it only where the snapshot's copy is newer. `ask` names each one and
    waits for `[y]es [N]o [a]ll [k]eep all [q]uit`.
    """
    sources = (source or []) + (group or [])
    # Resolved before the --to check: with several configs and no -c, a rehearsal printed first would fail as printed.
    config_path, config, _ = loaded(ctx)
    request = RestoreRequest(
        # Expanded here because no shell expands a quoted ~, or one after --to= in zsh. Without
        # --to, the request is the rehearsal the error below offers.
        to=str(Path(to).expanduser()) if to is not None else str(REHEARSAL_ROOT),
        from_date=from_date,
        all=all_sources,
        source=sources,
        tag=tag or [],
        on_conflict=on_conflict,
        skip_symlinked=skip_symlinked,
        dry_run=dry_run,
        no_input=invocation(ctx).no_input,
    )
    if to is None:
        rehearse = restore_command(request, config_path)
        ctx.fail(f'Missing --to: the root to restore into. --to / puts files back where they were.\nRehearse this one first: {rehearse}')
    do_restore(config, config_path, request)


# --- config -----------------------------------------------------------------------------------

config_app = typer.Typer(
    cls=Namespace,
    no_args_is_help=True,
    rich_markup_mode='markdown',
    help="""Write, read and change configs.

    A config names a destination, `back_up_to`, and the sources to copy there. Each has a name, and
    `-c NAME` picks one when there are several. `safekeep config show` names the file in use, and
    `safekeep config example` explains every key.
    """,
    epilog=examples(
        ('safekeep config init', 'a first config, named default'),
        ('safekeep config init work', 'a second config, which -c work then reads'),
        ('safekeep config show', 'the config in use, and which file it is'),
        ('safekeep config edit', 'change it, and have it checked when the editor closes'),
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
    """The config in use: which file it is, its destination and sources, and any warnings about it.

    Paths are shown with `~` and variables expanded, as a backup reads them.
    """
    config_path, config, warnings = loaded(ctx)
    show_config(config_path, config, warnings, as_json)


@config_app.command(
    'edit',
    epilog=examples(
        ('safekeep config edit', 'change the config, and have it checked when the editor closes'),
        ('safekeep -c work config edit', 'another config, by name'),
    ),
)
def config_edit(ctx: typer.Context) -> None:
    """Open the config in $VISUAL or $EDITOR, then check it.

    After the editor closes, the config is read again. An error or a warning in it is named then,
    rather than at the next backup, and a clean read counts its paths and git repos.
    """
    # Resolved but not loaded: a config that fails to load is the main reason to open one, and
    # load_config exits before the editor could fix it.
    edit_config(config_file(ctx))


@config_app.command(
    'init',
    epilog=examples(
        ('safekeep config init', 'a first config, named default'),
        ('safekeep config init work', 'a second config, after which every command names one with -c'),
        ('safekeep config init /mnt/backup/safekeep.toml', 'a config kept at a path of its own, read with -c and that path'),
    ),
)
def config_init(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(metavar='NAME|PATH', help='A name, or a .toml file to write. `-c` overrides it')] = 'default',
) -> None:
    """Write the annotated example as a new config.

    A name writes it beside the other configs, where `-c NAME` finds it. A path to a `.toml` file
    writes it there instead, and `-c` then takes that path. An existing config is never overwritten.
    The file it writes names an example destination and sources, to be changed with
    `safekeep config edit`.
    """
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
