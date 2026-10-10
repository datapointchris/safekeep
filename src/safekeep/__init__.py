"""safekeep - Config-driven file preservation with self-describing timestamped snapshots.

Rsync-copies files and directories to a destination as one snapshot per run, and writes a
manifest into each one recording what was collected, the source file modes, and which
sources were symlinks. That manifest is what makes a snapshot restorable without the
config that produced it -- the disaster-recovery case, where the config died with the
machine.

Primary use case: backing up scattered config files, local scripts, and git-untracked
WIP to a network drive that cannot represent Unix modes.

Unchanged files are hard-linked into the previous snapshot, so snapshots stay complete
trees while costing only what changed. That is why there is no retention policy.

Config: ~/.config/safekeep/<name>.toml (the manifest stays JSON -- machines write it)

Bare `safekeep` prints usage. Nothing writes without an explicit verb. The command tree is
`safekeep.main`. Its help screens are the one copy of the command surface: `safekeep --help`
for the tree, and each command's `-h` for its flags.
"""

import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatch
from functools import cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as installed_version
from pathlib import Path

from pytermstyle import bold
from pytermstyle import clip
from pytermstyle import cyan
from pytermstyle import green
from pytermstyle import red
from pytermstyle import yellow

# Keys are phrases that state what safekeep will do, so the file reads as a description of the
# backup rather than a dump of this program's variables -- see standards/configuration.md.
REQUIRED_KEYS = {'back_up_to'}
VALID_KEYS = {'back_up_to', 'back_up_paths', 'git', 'skip_names_matching', 'skip_files_over_mb'}
# 'repos' names the subject of this block; every other key states what happens to it.
VALID_REPO_KEYS = {'repos', 'back_up_untracked_files', 'back_up_ignored_files_matching'}

# Keys that once meant something. A generic "unknown key" warning is fine for a typo but
# useless for a key whose removal silently shrinks the backup, so retired keys carry their
# own message. Entries are deleted once every config has aged past them.
RETIRED_KEYS = {
    'keep': 'retention was removed — snapshots are no longer pruned; delete this key',
}

# Renamed keys are fatal where retired keys only warn: ignoring one of these backs up
# strictly less than the config asks for, and a backup that quietly shrinks goes unnoticed
# until a restore needs the files that are not there.
RENAMED_KEYS = {
    'dest': 'now "back_up_to"',
    'paths': 'now one [[back_up_paths]] block per path',
    'exclude': 'now "skip_names_matching"',
    'max_file_size_mb': 'now "skip_files_over_mb"',
    'repos': 'now a [git] table with one [[git.repos]] block per repo',
    'git_repos': 'now [git], and its "at" is now one [[git.repos]] block per repo',
    'git_untracked': 'now one [[git.repos]] block per repo, under [git]',
    'git_ignored': 'now "back_up_ignored_files_matching" under [git]',
}

DEFAULT_SKIP_NAMES = [
    '.venv',
    'node_modules',
    '__pycache__',
    '.mypy_cache',
    '.ruff_cache',
    '.pytest_cache',
    'build',
    'dist',
    '*.pyc',
    '.DS_Store',
    '.terraform',
]


def xdg_home(variable, fallback):
    """An XDG base directory: the variable when it holds an absolute path, else its default under home."""
    value = os.environ.get(variable, '')
    return Path(value) if os.path.isabs(value) else Path.home() / fallback


CONFIG_DIR = xdg_home('XDG_CONFIG_HOME', '.config') / 'safekeep'

# Where every command the tool prints for a reader to rehearse a restore aims it. A cache, because
# deleting it costs nothing, and the user's own, where a fixed path under /tmp is shared with
# every account on the machine.
REHEARSAL_ROOT = xdg_home('XDG_CACHE_HOME', '.cache') / 'safekeep' / 'rehearsal'

# How many snapshot names an error lists before it hands over to `snapshots list`.
SNAPSHOT_CHOICES_SHOWN = 5

SINGLE_TAG_EXAMPLE = 'tags = ["wsl"]'

# One snapshot per run, named for the second it started. The date half is still a plain date, so
# a --from that names a day still selects, and every snapshot written before this change is still
# found by the same pattern.
#
# No colons, because the primary destination is SMB and NTFS forbids them in a filename. That
# rules out strict ISO 8601, whose extended time separator is exactly what cannot be written --
# so the time is hyphenated to stay readable rather than run together as HHMMSS.
SNAPSHOT_FORMAT = '%Y-%m-%dT%H-%M-%S'
SNAPSHOT_NAME = re.compile(r'\d{4}-\d{2}-\d{2}(T\d{2}-\d{2}-\d{2})?')

MANIFEST_NAME = '.safekeep-manifest.json'
# 2 added the per-file lists on the git-derived groups, which is what lets a restore say that
# the file it just wrote was gitignored rather than untracked.
MANIFEST_VERSION = 2

# What each kind of group contributes, in words rather than in the manifest's key names. A git
# repo's line otherwise reads as though the repo itself is being restored -- which is the one
# thing a snapshot never holds, since a clone puts everything else back.
KIND_LABELS = {'path': 'path', 'git_untracked': 'untracked', 'git_ignored': 'ignored'}

PRE_RESTORE_SUFFIX = '.pre-restore'


def tool_version() -> str:
    """This build's version, or 'unknown' when running from a source checkout.

    Read from the installed distribution metadata rather than a constant in this
    file, so there is one version and semantic-release owns it. A source checkout
    that was never installed has no metadata, and says so instead of inventing a
    number -- release.md is explicit that a version string can never be used to
    tell a release from a dev build.
    """
    try:
        return installed_version('safekeep')
    except PackageNotFoundError:
        return 'unknown'


# Destination is typically SMB/DrvFs, which cannot store Unix modes, so the backup is
# written with --no-perms and every file arrives with the same mode. Restore reapplies
# these defaults and then the recorded deviations, which is why the manifest only needs
# to carry the interesting entries (0600 secrets, +x scripts) rather than every file.
DEFAULT_FILE_MODE = 0o644
DEFAULT_DIR_MODE = 0o755

# Written verbatim by `init`. tomllib reads but does not write, and that is the better
# half of the trade: a serialized dict cannot carry comments, and the comments are the
# point -- this file is meant to be read as a description of the backup.
CONFIG_TEMPLATE = """\
# safekeep configuration. Every key states what safekeep will do with it.
# This file is the reference -- the comments below explain every key.

back_up_to = "/mnt/h/backups"

# Patterns no backup ever copies, matched against any single path component.
# Delete this key entirely to accept the defaults (.venv, node_modules, the usual
# caches, *.pyc, .DS_Store, build, dist, .terraform).
skip_names_matching = [".venv", "node_modules", "*.pyc", "*.iso"]

# Files larger than this are skipped, and each one is named in the snapshot
# manifest so a short backup is never silently short.
skip_files_over_mb = 50

# One [[back_up_paths]] block per path, each copied whole. Tags are free-form
# labels: `safekeep restore --tag secrets` restores just those sources, so tag by
# the scenario you would restore in, not by what the files are.

[[back_up_paths]]
path = "~/.ssh"
tags = ["secrets", "rebuild"]

[[back_up_paths]]
path = "~/.config/gnupg"
tags = ["secrets", "rebuild"]

# A single file is as valid as a directory.
[[back_up_paths]]
path = "~/.gitconfig"
tags = ["rebuild"]

[[back_up_paths]]
path = "~/notes"
tags = ["notes"]

# Anything reachable on this machine, not just $HOME. On WSL that includes the
# Windows side, which is why the tag exists -- it will not apply on a rebuild
# onto Linux, so it is worth being able to leave behind.
[[back_up_paths]]
path = "/mnt/c/Users/me/Documents/work-notes"
tags = ["windows"]

# Everything under [git] applies to every repo listed below it. These two keys
# must come before the first [[git.repos]] block: TOML closes a table as soon as
# a subtable opens, so anything after the blocks would be read as part of one.
[git]

# Untracked files are the point of listing a repo at all -- a clone brings back
# everything else. Set false to take only the ignored patterns below.
back_up_untracked_files = true

# Gitignored files worth keeping anyway, matched in every repo below. A pattern
# matches a whole repo-relative path or any single component of it, so
# ".planning" catches everything beneath a .planning/ directory at any depth,
# and "*.env" catches a stray secret wherever it sits.
back_up_ignored_files_matching = ["CLAUDE.md", ".planning", "*.env"]

[[git.repos]]
path = "~/dotfiles"
tags = ["rebuild"]

[[git.repos]]
path = "~/code/side-project"
tags = ["wip"]

[[git.repos]]
path = "~/work/client-api"
tags = ["wip", "work"]
"""


class ConflictPolicy(StrEnum):
    """What a restore does with a file already at the target."""

    BACKUP = 'backup'
    SKIP = 'skip'
    OVERWRITE = 'overwrite'
    NEWER = 'newer'
    ASK = 'ask'


@dataclass(frozen=True)
class BackupRequest:
    """What one `backup run` was asked to cover."""

    tag: list[str]
    source: list[str]
    # None when --label was not typed. `--label ''` is a typed decision, and the manifest records it.
    label: str | None
    dry_run: bool


@dataclass(frozen=True)
class RestoreRequest:
    """What one `restore` was asked to bring back, and where to."""

    to: str
    from_date: str | None
    all: bool
    source: list[str]
    tag: list[str]
    on_conflict: ConflictPolicy
    skip_symlinked: bool
    dry_run: bool
    no_input: bool


def plural(count, noun):
    return f'{count} {noun}' if count == 1 else f'{count} {noun}s'


def print_json(value):
    """Emit a read's --json answer, the only thing it writes to stdout."""
    print(json.dumps(value, indent=2))


def human_size(num_bytes):
    if num_bytes >= 1024**3:
        return f'{num_bytes / 1024**3:.2f} GB'
    if num_bytes >= 1024**2:
        return f'{num_bytes / 1024**2:.1f} MB'
    if num_bytes >= 1024:
        return f'{num_bytes / 1024:.1f} KB'
    return f'{num_bytes} B'


def status(message):
    """Overwrite the current line with a live status, on a terminal only.

    A carriage return in a redirected log collapses the whole run into one unreadable line,
    and a restore is exactly the thing run under tee. Every phase that can print one of these
    also prints an ordinary line when it finishes, so a log loses the motion and keeps the report.

    Clipped to the terminal, because a message that wraps is two lines and the carriage return
    only returns to the start of the second — leaving the first behind on every redraw.
    """
    if sys.stdout.isatty():
        print(f'\r\033[K  {clip(message, 2)}', end='', flush=True)


def clear_status():
    if sys.stdout.isatty():
        print('\r\033[K', end='', flush=True)


def clip_to_terminal(text, used):
    """`text` shortened to the room left on a terminal line, and untouched off a terminal.

    Redirected output has no width to fit, and clip falls back to 80 columns rather than
    declining -- so an unguarded call truncates a captured log to a width nothing asked for.
    Same reasoning as `status` above: a redirection keeps the report and loses only the fit.
    """
    return clip(text, used) if sys.stdout.isatty() else text


def warn_about_json_configs():
    """Name the leftover JSON configs, since 'no configs found' is a bewildering way to
    report a format change to someone whose config file is sitting right there."""
    leftovers = sorted(CONFIG_DIR.glob('*.json')) if CONFIG_DIR.exists() else []
    if not leftovers:
        return
    print(f'  {yellow("configs are TOML now")}, and these are still JSON:', file=sys.stderr)
    for path in leftovers:
        print(f'    {path.name} -> {path.stem}.toml', file=sys.stderr)


def names_a_path(name):
    """Whether a config was named by a file rather than by name: a directory part, a ~, or a .toml suffix."""
    return os.sep in name or name.startswith('~') or name.endswith('.toml')


def config_names():
    return sorted(path.stem for path in CONFIG_DIR.glob('*.toml')) if CONFIG_DIR.exists() else []


def print_first_config_route():
    """How to get a first config, including the case of a machine that has only the backup drive."""
    print(f'  write one: {cyan("safekeep config init")}', file=sys.stderr)
    print('  to read an existing backup drive, set its back_up_to to the directory holding the snapshots.', file=sys.stderr)
    print('  a restore needs nothing else from the config', file=sys.stderr)


def holds_snapshots(directory):
    try:
        return any(child.is_dir() and SNAPSHOT_NAME.fullmatch(child.name) for child in directory.iterdir())
    except OSError:
        return False


def retyped_with_config(typed, name):
    """The command line as typed, with `-c name` first in place of any -c it had."""
    typed = list(typed)
    root, at = [], 0
    while at < len(typed) and typed[at].startswith('-'):
        word = typed[at]
        if word in ('-c', '--config'):
            at += 2
            continue
        if not (word.startswith('--config=') or (word.startswith('-c') and not word.startswith('--'))):
            root.append(word)
        at += 1
    return shlex.join(['safekeep', '-c', name, *root, *typed[at:]])


def resolve_config(name, typed=()):
    """The config file a command reads: named, at a path, or the only one there is.

    `typed` is the command line as given, so an error can print it back with the -c that works.
    """
    if name and names_a_path(name):
        # Absolute, so every command printed for this config runs from any directory.
        path = Path(name).expanduser().absolute()
        if path.is_dir():
            print(f'{red("safekeep:")} {yellow(str(path))} is a directory, and -c takes a config name or a .toml file', file=sys.stderr)
            if holds_snapshots(path):
                print('  it holds snapshots. To read them, write a config whose back_up_to is this directory:', file=sys.stderr)
                print(f'    {cyan("safekeep config init drive")}', file=sys.stderr)
                print(f'    {cyan("safekeep -c drive config edit")}', file=sys.stderr)
                print('  a restore needs nothing else from the config', file=sys.stderr)
            sys.exit(2)
        if path.is_file():
            return path
        print(f'{red("safekeep:")} no config file at {yellow(str(path))}', file=sys.stderr)
        # A bare file name usually means the config of that name, which -c takes without the suffix.
        if os.sep not in name and (CONFIG_DIR / Path(name).name).is_file():
            print(
                f'  the config named {green(Path(name).stem)} is that file, by name: {cyan(retyped_with_config(typed, Path(name).stem))}',
                file=sys.stderr,
            )
        elif path.parent.is_dir():
            print(f'  write one there: {cyan(f"safekeep config init {shell_path(str(path))}")}', file=sys.stderr)
        else:
            print(f'  and no directory {yellow(str(path.parent))} to write one in', file=sys.stderr)
        sys.exit(1)

    if name:
        config_path = CONFIG_DIR / f'{name}.toml'
        if config_path.exists():
            return config_path
        print(f'{red("safekeep:")} no config named {yellow(name)} in {cyan(str(CONFIG_DIR))}', file=sys.stderr)
        known = config_names()
        print(f'  configs: {green(", ".join(known)) if known else yellow("none")}', file=sys.stderr)
        warn_about_json_configs()
        print(f'  write a new one: {cyan(f"safekeep config init {shlex.quote(name)}")}', file=sys.stderr)
        sys.exit(1)

    configs = config_names()
    if not configs:
        print(f'{red("safekeep:")} no configs in {cyan(str(CONFIG_DIR))}', file=sys.stderr)
        warn_about_json_configs()
        print_first_config_route()
        sys.exit(1)
    if len(configs) == 1:
        return CONFIG_DIR / f'{configs[0]}.toml'

    print(f'{red("safekeep:")} {len(configs)} configs, so name one before the command: {green(", ".join(configs))}', file=sys.stderr)
    print(f'  {cyan(retyped_with_config(typed, configs[0]))}', file=sys.stderr)
    sys.exit(2)


def load_config(config_path):
    """Load a config, returning (config, warnings).

    Missing required keys are fatal. Unrecognized keys warn and are ignored, so the
    config can be edited ahead of the tool without breaking a backup run.
    """
    try:
        with open(config_path, 'rb') as f:
            config = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        print(f'{red("safekeep:")} {cyan(str(config_path))} is not valid TOML — {e}', file=sys.stderr)
        sys.exit(1)

    # Renames are reported before missing keys: an old config is missing the required key
    # *because* it was renamed, and "back_up_to is missing" is the least useful way to say so.
    renamed = sorted(set(config.keys()) & set(RENAMED_KEYS))
    if renamed:
        for key in renamed:
            print(f'{red("safekeep:")} config key {yellow(repr(key))} was renamed: {RENAMED_KEYS[key]}', file=sys.stderr)
        print(f'  edit {cyan(str(config_path))}, then re-run', file=sys.stderr)
        sys.exit(1)

    missing = REQUIRED_KEYS - set(config.keys())
    if missing:
        for key in sorted(missing):
            print(f'{red("safekeep:")} config missing required key {yellow(repr(key))}: {cyan(str(config_path))}', file=sys.stderr)
        sys.exit(1)

    warnings = []
    for key in sorted(set(config.keys()) - VALID_KEYS):
        if key in RETIRED_KEYS:
            warnings.append(f'{key}: {RETIRED_KEYS[key]}')
        else:
            warnings.append(f'{key}: unrecognized key, ignored')

    repos = config.get('git', {})
    if not isinstance(repos, dict):
        print(f'{red("safekeep:")} {yellow("git")} must be a [git] table whose [[git.repos]] blocks list the repos', file=sys.stderr)
        sys.exit(1)
    for key in sorted(set(repos.keys()) - VALID_REPO_KEYS):
        warnings.append(f'git.{key}: unrecognized key, ignored')
    if not repos.get('repos'):
        for key in ('back_up_untracked_files', 'back_up_ignored_files_matching'):
            if repos.get(key):
                warnings.append(f'git.{key}: no repos listed in git.repos, so it does nothing')

    return config, warnings


def repo_entries(config):
    """Return the repos and what to take from each, as ([(path, tags)], untracked, patterns)."""
    repos = config.get('git', {})
    return (
        normalize_entries(repos.get('repos', [])),
        repos.get('back_up_untracked_files', True),
        repos.get('back_up_ignored_files_matching', []),
    )


def normalize_entries(entries):
    """Normalize a list of [[back_up_paths]]/[[git.repos]] blocks into [(expanded_path, tags)].

    Every entry is a table with 'path' and optional 'tags'. Under JSON an entry could also
    be a bare string, which meant two shapes to write and two to parse; an array of tables
    is uniform and gives every entry a line of its own to be commented on.
    """
    normalized = []
    for entry in entries:
        if not isinstance(entry, dict) or 'path' not in entry:
            print(
                f'{red("safekeep:")} every [[back_up_paths]] and [[git.repos]] table needs a "path" key: {yellow(repr(entry))}',
                file=sys.stderr,
            )
            sys.exit(1)
        tags = entry.get('tags', [])
        # Fatal rather than coerced: a bare tags = "wsl" is a list of characters to Python, so
        # the entry ends up tagged w, s and l, and the only symptom is `restore --tag wsl`
        # selecting nothing from a snapshot whose config plainly carries the tag.
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            print(f'{red("safekeep:")} "tags" must be a list of strings: {yellow(repr(entry))}', file=sys.stderr)
            print(f'  a single tag is still a list: {cyan(SINGLE_TAG_EXAMPLE)}', file=sys.stderr)
            sys.exit(1)
        # $VARIABLES before ~, because a config may name a path it must not carry. A
        # file whose location differs per machine is declared as a variable and set on
        # each one, so the same config text backs up the right file everywhere — the
        # generator emitting it has no business resolving another machine's answer.
        # An unset variable stays literal and the path simply will not exist, which is
        # reported as a missing path rather than passing silently.
        normalized.append((Path(os.path.expandvars(entry['path'])).expanduser(), list(tags)))
    return normalized


def git_env():
    """The environment with every GIT_* variable removed.

    `cwd` alone does not decide which repository git reads. An inherited GIT_DIR
    or GIT_INDEX_FILE overrides it, and git exports both to every hook it runs —
    so a backup triggered from a hook, a `git rebase --exec`, or a pre-commit run
    would list another repository's files while appearing to succeed. For a backup
    tool that is the worst failure mode available: the wrong file set, silently,
    with a zero exit code.
    """
    return {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}


def git_ls_untracked(repo_path):
    """Get list of untracked files from a git repository."""
    try:
        result = subprocess.run(
            ['git', 'ls-files', '--others', '--exclude-standard'],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
            env=git_env(),
        )
        return [repo_path / line for line in result.stdout.strip().splitlines() if line]
    except (subprocess.CalledProcessError, FileNotFoundError):
        print(f'  {yellow("warning:")} could not list untracked files in {cyan(str(repo_path))}')
        return []


def git_ls_ignored(repo_path, patterns):
    """Get list of gitignored files matching patterns from a git repository.

    Uses git ls-files --others (without --exclude-standard) and subtracts the
    --exclude-standard set to get only ignored files, then filters to those
    matching the given glob patterns.
    """
    try:
        all_result = subprocess.run(
            ['git', 'ls-files', '--others'],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
            env=git_env(),
        )
        untracked_result = subprocess.run(
            ['git', 'ls-files', '--others', '--exclude-standard'],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
            env=git_env(),
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        print(f'  {yellow("warning:")} could not list ignored files in {cyan(str(repo_path))}')
        return []

    all_files = set(all_result.stdout.strip().splitlines())
    untracked_files = set(untracked_result.stdout.strip().splitlines())
    ignored_files = all_files - untracked_files

    matched = []
    for rel_path in sorted(ignored_files):
        if not rel_path:
            continue
        for pattern in patterns:
            if fnmatch(rel_path, pattern) or any(fnmatch(part, pattern) for part in Path(rel_path).parts):
                matched.append(repo_path / rel_path)
                break

    return matched


def matches_exclude(rel_path, excludes):
    """Check if any path component matches an exclude pattern."""
    for part in Path(rel_path).parts:
        for pattern in excludes:
            if fnmatch(part, pattern):
                return True
    return False


def snapshot_rel(path):
    """Map an absolute source path to its location inside a snapshot."""
    return str(path).lstrip('/')


def record_file(path, survey, max_size_mb):
    """Stat one file into the survey, returning True if it will be copied."""
    try:
        stat = path.stat()
    except OSError:
        return False

    if max_size_mb is not None and stat.st_size > max_size_mb * 1024 * 1024:
        survey['skipped_large'].append({'path': str(path), 'mb': round(stat.st_size / (1024 * 1024), 1)})
        return False

    survey['files'] += 1
    survey['bytes'] += stat.st_size
    mode = stat.st_mode & 0o777
    if mode != DEFAULT_FILE_MODE:
        survey['modes'][snapshot_rel(path)] = f'{mode:04o}'
    return True


def survey_tree(root, excludes, max_size_mb):
    """Walk a source path recording sizes, modes, and symlink origins.

    Follows symlinked directories because the backup dereferences them (rsync -L), and
    tracks resolved directories to keep a symlink cycle from hanging the walk.
    """
    survey = {'files': 0, 'bytes': 0, 'modes': {}, 'symlinks': {}, 'skipped_large': []}

    if root.is_symlink():
        survey['symlinks'][snapshot_rel(root)] = os.readlink(root)

    if not root.exists():
        return survey

    if root.is_file():
        record_file(root, survey, max_size_mb)
        return survey

    seen_dirs = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        current = Path(dirpath)
        resolved = current.resolve()
        if resolved in seen_dirs:
            dirnames[:] = []
            continue
        seen_dirs.add(resolved)

        dirnames[:] = [d for d in dirnames if not matches_exclude(d, excludes)]

        try:
            dir_mode = current.stat().st_mode & 0o777
        except OSError:
            dir_mode = DEFAULT_DIR_MODE
        # The source's own directory is recorded like any other. Excluding it meant a restore
        # created ~/.ssh and ~/.config/gnupg at the 0755 default, and gpg refuses a homedir
        # anyone can read -- the one mode in the tree that most had to survive was the one
        # nothing wrote down.
        if dir_mode != DEFAULT_DIR_MODE:
            survey['modes'][snapshot_rel(current)] = f'{dir_mode:04o}'

        for name in dirnames:
            child = current / name
            if child.is_symlink():
                survey['symlinks'][snapshot_rel(child)] = os.readlink(child)

        for name in filenames:
            if matches_exclude(name, excludes):
                continue
            child = current / name
            if child.is_symlink():
                survey['symlinks'][snapshot_rel(child)] = os.readlink(child)
            record_file(child, survey, max_size_mb)

    return survey


def survey_files(files, max_size_mb):
    """Stat an explicit file list (git-derived) into a survey."""
    survey = {'files': 0, 'bytes': 0, 'modes': {}, 'symlinks': {}, 'skipped_large': []}
    for path in files:
        if path.is_symlink():
            survey['symlinks'][snapshot_rel(path)] = os.readlink(path)
        record_file(path, survey, max_size_mb)
    return survey


def merge_survey(manifest, survey):
    manifest['modes'].update(survey['modes'])
    manifest['symlinks'].update(survey['symlinks'])
    manifest['skipped_large'].extend(survey['skipped_large'])


# A file that changed this run is a fresh copy however much else linked, so one sample can miss
# real sharing. Bounded rather than exhaustive because proving a negative means walking the whole
# tree, and every stat is a network round trip. Sharing is found in the first few files when it
# is happening at all, so the bound only costs anything on a destination that cannot link.
LINK_PROBE_LIMIT = 200


def link_source_of(snapshot_dir, link_dest):
    """The name of the snapshot this one is observed to share inodes with, or None.

    Asking rsync for --link-dest is not evidence it happened. The option is absent on openrsync,
    and a destination filesystem can refuse link() and leave rsync copying instead -- neither is
    reported, and the run succeeds either way. SMB without Unix extensions is exactly that case,
    and it is the primary destination.

    So this reads the inodes rather than the flag. Recording the flag meant the manifest named a
    snapshot it might share nothing with, which is the one field that could have answered whether
    the linking works here.
    """
    if link_dest is None:
        return None
    checked = 0
    for dirpath, _, filenames in os.walk(snapshot_dir):
        for name in filenames:
            here = Path(dirpath) / name
            there = Path(link_dest) / here.relative_to(snapshot_dir)
            try:
                if here.stat().st_ino == there.stat().st_ino:
                    return Path(link_dest).name
            except OSError:
                continue
            checked += 1
            if checked >= LINK_PROBE_LIMIT:
                return None
    return None


def link_dest_flags(link_dest):
    """--link-dest against the previous snapshot, when there is one and this rsync has it.

    Unchanged files then cost a hard link rather than a copy, so keeping every snapshot forever
    stays affordable — which is the policy, snapshots are never pruned. Absolute, because rsync
    resolves a relative --link-dest against the destination directory and the two are siblings.

    Silently absent on openrsync, which has no --link-dest: the snapshot is a full copy instead,
    identical in content and restorable by the same code. Degrading is the whole point of asking.
    """
    if link_dest is None or not rsync_supports('--link-dest'):
        return []
    return [f'--link-dest={Path(link_dest).resolve()}']


def rsync_paths(paths, dest_base, excludes, dry_run=False, max_size_mb=None, link_dest=None):
    """Rsync absolute paths into dest_base, preserving full directory structure; returns the files copied.

    Uses rsync --relative with absolute paths so that '/home/chris/.ssh/config'
    becomes dest_base/home/chris/.ssh/config.
    """
    valid = [str(p) for p in paths if p.exists()]
    if not valid:
        return 0

    if not dry_run:
        dest_base.mkdir(parents=True, exist_ok=True)

    cmd = ['rsync', '-a', '--no-perms', '--chmod=Du+w', '--relative', '--copy-links', *rsync_naming_flags()]
    cmd.extend(link_dest_flags(link_dest))
    for pattern in excludes:
        cmd.extend(['--exclude', pattern])
    if max_size_mb is not None:
        cmd.extend(['--max-size', f'{max_size_mb}m'])
    if dry_run:
        cmd.append('-n')
    cmd.extend(valid)
    cmd.append(str(dest_base) + '/')

    return run_backup_rsync(cmd)


def rsync_untracked(files, dest_base, dry_run=False, link_dest=None):
    """Rsync individual untracked files preserving full path structure; returns the files copied.

    Uses --files-from with / as the base for efficiency when copying many
    small files. Paths are stored relative to filesystem root in the destination.
    """
    rel_paths = [snapshot_rel(f) for f in files]
    if not rel_paths:
        return 0

    if not dry_run:
        dest_base.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as tmp:
        tmp.write('\n'.join(rel_paths) + '\n')
        tmp_path = tmp.name

    try:
        cmd = ['rsync', '-a', '--no-perms', *rsync_naming_flags(), '--files-from', tmp_path, '/', str(dest_base) + '/']
        cmd.extend(link_dest_flags(link_dest))
        if dry_run:
            cmd.append('-n')
        return run_backup_rsync(cmd)
    finally:
        os.unlink(tmp_path)


def copy_tally(dry_run, copied, origin):
    """One backup section's last line: the files rsync named as it copied them."""
    verb = yellow('would copy') if dry_run else green('copied')
    return f'{verb} {bold(plural(copied, "file"))} from {origin}'


def link_verdict(linked_from):
    """Where a snapshot's unchanged files went, read from inodes, in the words every command uses."""
    return f'hard links into {linked_from}' if linked_from else 'copied in full, linked to no earlier snapshot'


def run_backup_rsync(cmd):
    """Run one backup rsync, naming each file it copies by the path it came from, and count them.

    rsync names only what it writes, so a file unchanged since the previous snapshot is a hard
    link and goes unnamed and uncounted.
    """
    copied = []

    def report(name):
        copied.append(name)
        print(f'    {tilde("/" + name.lstrip("/"))}', flush=True)

    run_rsync_naming_files(cmd, report)
    return len(copied)


@cache
def rsync_help():
    """This rsync's own option list, which is the only reliable thing to ask it about.

    A restore does not get to assume the good rsync: macOS ships openrsync as /usr/bin/rsync,
    and a disaster recovery is precisely the moment the Homebrew rsync this repo declares has
    not been installed yet. Both print their options, so both can be asked.
    """
    probe = subprocess.run(['rsync', '--help'], capture_output=True, text=True)
    return probe.stdout + probe.stderr


def rsync_supports(option):
    return option in rsync_help()


# Only the -v fallback needs these: --out-format prints the paths and nothing else, while -v
# wraps them in a preamble and a transfer summary. A sentinel prefix would be the exact answer
# and is not available -- rsync escapes any non-printable byte in its own output, mark included.
RSYNC_NOISE = (
    'sending incremental file list',
    'receiving incremental file list',
    'building file list',
    'sent ',
    'received ',
    'total size',
    'created directory',
    'done',
)


def rsync_naming_flags():
    """The flags that make rsync name each path it writes, and stream them a line at a time.

    --out-format is the exact answer and is what rsync 3.x gets; -v is the same list buried in
    summary lines, and is all openrsync offers. Without --outbuf the output is block-buffered
    into a pipe, and a restore that reports in 4 KB bursts is no better than one that says
    nothing until it finishes.
    """
    flags = []
    if rsync_supports('--out-format'):
        flags.append('--out-format=%n')
    else:
        flags.append('-v')
    if rsync_supports('--outbuf'):
        flags.append('--outbuf=L')
    return flags


def rsync_named_file(line):
    """The path rsync just wrote, from one line of its output, or None if the line is not one.

    Directories are dropped: rsync names them with a trailing slash, and a restore reports the
    files it put back rather than the tree it had to create to hold them.
    """
    if not rsync_supports('--out-format') and line.startswith(RSYNC_NOISE):
        return None
    if not line or line == './' or line.endswith('/'):
        return None
    return line


def check_rsync(cmd, returncode):
    """Tolerate the partial-transfer exit codes; exit on anything else.

    Any other failure exits rather than raising: a traceback names the Python frame that called
    rsync, which is never the thing that went wrong, and it buries the command. Re-running that
    command by hand is how an rsync failure gets diagnosed, so it is what the error prints.
    """
    if returncode in (23, 24):
        print(f'  {yellow("warning:")} rsync completed with partial transfer (some files skipped)')
        return
    if returncode != 0:
        print(f'{red("safekeep:")} rsync exited {yellow(str(returncode))}', file=sys.stderr)
        print(f'  {shlex.join(cmd)}', file=sys.stderr)
        sys.exit(1)


def run_rsync_naming_files(cmd, report):
    """Run rsync, handing `report` each path as rsync writes it."""
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    for line in process.stdout:
        name = rsync_named_file(line.rstrip('\n'))
        if name is not None:
            report(name)
    check_rsync(cmd, process.wait())


def read_manifest(snapshot_dir):
    """Read a snapshot's manifest, or None if it has none."""
    manifest_path = Path(snapshot_dir) / MANIFEST_NAME
    if not manifest_path.exists():
        return None
    try:
        with open(manifest_path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def list_snapshots(dest):
    """List snapshot directories at dest, newest first, paired with manifests."""
    if not dest.exists():
        return []
    dated = [d for d in dest.iterdir() if d.is_dir() and SNAPSHOT_NAME.fullmatch(d.name)]
    return [(d, read_manifest(d)) for d in sorted(dated, key=lambda d: d.name, reverse=True)]


def resolve_snapshot(dest, wanted):
    """The snapshot `wanted` names: that name exactly, or the newest run of a day it prefixes.

    Nobody types 2026-08-13T17-04-32 from memory, and a date is what a person actually has -- so
    a date resolves to the last run that day, which is what --from almost always means. Every
    caller prints the name it resolved to rather than what was typed, because a prefix that
    matched something unintended is only visible if the answer says which snapshot it picked.
    """
    snapshots = [d for d, _ in list_snapshots(dest)]
    exact = [d for d in snapshots if d.name == wanted]
    return exact[0] if exact else next((d for d in snapshots if d.name.startswith(wanted)), None)


def snapshot_choices(dest, config_path):
    """The snapshots an error asking for one can offer, newest first, as plain text lines."""
    names = [snapshot_dir.name for snapshot_dir, _ in list_snapshots(dest)]
    command = safekeep_for(config_path)
    if not names:
        return f'No snapshots at {dest} yet. Take one: {command} backup run'
    lines = [f'  {name}' + ('  (newest)' if position == 0 else '') for position, name in enumerate(names[:SNAPSHOT_CHOICES_SHOWN])]
    hidden = len(names) - SNAPSHOT_CHOICES_SHOWN
    if hidden > 0:
        lines.append(f'  and {hidden} more: {command} snapshots list')
    return 'Snapshots:\n' + '\n'.join(lines)


def unreadable_reason(dest, wanted, snapshot_dir, config_path):
    """Why one snapshot cannot be read: absent, with the ones there are, or present without a manifest."""
    if snapshot_dir is None:
        return f'no snapshot {wanted} at {dest}\n{snapshot_choices(dest, config_path)}'
    return f'snapshot {snapshot_dir.name} has no manifest, so safekeep can neither read nor restore it'


def fail_unreadable(dest, wanted, snapshot_dir, config_path):
    print(f'{red("safekeep:")} {unreadable_reason(dest, wanted, snapshot_dir, config_path)}', file=sys.stderr)
    sys.exit(1)


def previous_snapshot(dest, snapshot_name):
    """The newest snapshot to hard-link unchanged files against, or None on the first run.

    This run's own directory is excluded. Its name carries a timestamp so it normally does not
    exist yet, but two runs inside one second share a name -- and linking a tree against itself
    is both meaningless and a way to lose the freshly-copied version of a file that changed.
    """
    for snapshot_dir, _ in list_snapshots(dest):
        if snapshot_dir.name != snapshot_name:
            return snapshot_dir
    return None


def group_id(group):
    return f'{group["kind"]}:{group["source"]}'


def kinds_label(kinds):
    return ' + '.join(KIND_LABELS.get(kind, kind) for kind in kinds)


def source_rows(groups):
    """One row per source, sorted by path — the unit a restore actually works in.

    A repo contributes an untracked group and an ignored group over one subtree, so restoring
    per group would rsync it twice; they are disjoint file sets, so their counts sum. Offering
    the two separately was a lie besides, since selecting either restored both.

    Sorted by path because that is the order the eye can scan a picker of thirty sources in —
    manifest order is config order, which is only meaningful to whoever wrote the config.
    """
    rows = {}
    for group in groups:
        row = rows.setdefault(group['source'], {'source': group['source'], 'kinds': [], 'tags': [], 'files': 0, 'bytes': 0})
        if group['kind'] not in row['kinds']:
            row['kinds'].append(group['kind'])
        row['tags'] += [tag for tag in group.get('tags', []) if tag not in row['tags']]
        row['files'] += group.get('files', 0)
        row['bytes'] += group.get('bytes', 0)
    return sorted(rows.values(), key=lambda row: row['source'])


def file_kinds(manifest):
    """{snapshot-relative path: label} for the files whose group recorded a list of them.

    Only the git-derived groups record one. A path group's files are all the same kind, so a
    list would say nothing a source line has not already said — and listing whole directory
    trees would leave the manifest mostly filenames.
    """
    kinds = {}
    for group in manifest.get('groups', []):
        for rel in group.get('paths', []):
            kinds[rel] = KIND_LABELS.get(group['kind'], group['kind'])
    return kinds


def snapshot_name_width(snapshots):
    """The widest snapshot name in a listing, so the columns after it line up.

    A destination carries both name shapes at once: every snapshot taken before the time was
    added is a bare date, ten characters against nineteen.
    """
    return max((len(snapshot_dir.name) for snapshot_dir, _ in snapshots), default=0)


def snapshot_summary(snapshot_dir, manifest):
    """One snapshot as `snapshots list` reports it. A manifestless one is known by its name alone."""
    summary = {'snapshot': snapshot_dir.name, 'restorable': manifest is not None}
    if manifest is None:
        return summary | dict.fromkeys(('created', 'host', 'label', 'files', 'bytes', 'source_count', 'linked_from'))
    groups = manifest.get('groups', [])
    return summary | {
        'created': manifest.get('created'),
        'host': manifest.get('hostname'),
        'label': manifest.get('label'),
        'files': sum(g.get('files', 0) for g in groups),
        'bytes': sum(g.get('bytes', 0) for g in groups),
        'source_count': len(source_rows(groups)),
        'linked_from': manifest.get('linked_from'),
    }


def counted(count, noun, width):
    """A count right-aligned to `width` and its noun padded to its plural, so a column of them lines up."""
    return f'{count:>{width}} {noun if count == 1 else noun + "s":<{len(noun) + 1}}'


def show_snapshot_list(dest, config_path, as_json=False):
    snapshots = list_snapshots(dest)
    if as_json:
        print_json([snapshot_summary(snapshot_dir, manifest) for snapshot_dir, manifest in snapshots])
        return
    if not snapshots:
        print(f'{yellow("safekeep:")} no snapshots at {cyan(str(dest))}')
        print(f'  take one: {cyan(f"{safekeep_for(config_path)} backup run")}')
        return

    print(f'{bold("safekeep:")} {plural(len(snapshots), "snapshot")} at {cyan(str(dest))}')
    print()
    # Padded across the listing rather than to a constant: a destination holds names of both
    # shapes, since every snapshot taken before the time was added is a bare date. A shorter
    # name left unpadded shifts every column on its row and the listing stops being scannable.
    width = snapshot_name_width(snapshots)
    for snapshot_dir, manifest in snapshots:
        name = f'{snapshot_dir.name:<{width}}'
        summary = snapshot_summary(snapshot_dir, manifest)
        if not summary['restorable']:
            print(f'  {bold(name)}  {yellow("no manifest, so safekeep can neither read nor restore it")}')
            continue
        host = summary['host'] or '?'
        sizes = (
            f'{human_size(summary["bytes"]):>9}  {counted(summary["files"], "file", 6)}  {counted(summary["source_count"], "source", 2)}'
        )
        cells = f'{name}  {sizes}  {host}'
        row = f'  {bold(name)}  {sizes}  {cyan(host)}'
        # Free text of any length, so it goes last and is clipped against the columns before it:
        # a row that wraps is two rows, and a column of dates stops being scannable the moment
        # one of them is not at the left. On a terminal only, because a redirected run has no
        # width to fit and loses data if one is assumed. clip measures
        # uncolored text, which is what `cells` is for.
        if summary['label']:
            row += '  ' + green(clip_to_terminal(summary['label'], len(cells) + 4))
        print(row)


def show_snapshot_record(dest, date, config_path, as_json=False):
    """What one snapshot holds, as the manifest records it — also the fzf preview pane."""
    snapshot_dir = resolve_snapshot(dest, date)
    manifest = read_manifest(snapshot_dir) if snapshot_dir else None
    if manifest is None:
        fail_unreadable(dest, date, snapshot_dir, config_path)
    if as_json:
        home_then = manifest.get('home')
        home_now = str(Path.home())
        print_json(
            snapshot_summary(snapshot_dir, manifest)
            | {
                'config_name': manifest.get('config_name'),
                'home': home_then,
                'sources': [
                    {
                        'source': remap_home(row['source'], home_then, home_now),
                        'recorded': row['source'],
                        'kinds': [KIND_LABELS.get(kind, kind) for kind in row['kinds']],
                        'tags': row['tags'],
                        'files': row['files'],
                        'bytes': row['bytes'],
                    }
                    for row in source_rows(manifest.get('groups', []))
                ],
                'skipped_large': manifest.get('skipped_large', []),
                'config_warnings': manifest.get('config_warnings', []),
            }
        )
        return
    print(f'{snapshot_dir.name}   {manifest.get("hostname", "?")}   {manifest.get("created", "?")}')
    print(f'config: {manifest.get("config_name", "?")}   home: {manifest.get("home", "?")}')
    if manifest.get('label'):
        print(f'label: {manifest["label"]}')
    # Named because it is what says whether this destination can hard-link at all. A run of
    # snapshots all copied in full means every one of them costs its full size.
    print(f'unchanged files: {link_verdict(manifest.get("linked_from"))}')
    print()
    for row in source_rows(manifest.get('groups', [])):
        tags = ' '.join(row['tags'])
        print(f'  {kinds_label(row["kinds"]):<14} {row["source"]}')
        print(f'  {"":<14} {plural(row["files"], "file")}, {human_size(row["bytes"])}  {tags}')
    skipped = manifest.get('skipped_large', [])
    if skipped:
        print()
        print(f'  {plural(len(skipped), "file")} skipped for exceeding skip_files_over_mb')
    warnings = manifest.get('config_warnings', [])
    if warnings:
        print()
        for warning in warnings:
            print(f'  config warning: {warning}')


def config_entries(config):
    """Every config entry as (kind, path, tags) — the paths first, then the git repos."""
    entries = [('path', path, tags) for path, tags in normalize_entries(config.get('back_up_paths', []))]
    repos, _, _ = repo_entries(config)
    return entries + [('git repo', path, tags) for path, tags in repos]


def snapshot_to_size_against(dest, date, config_path):
    """The snapshot a tag listing reports against: the one named, or the newest restorable one."""
    snapshots = [(d, m) for d, m in list_snapshots(dest) if m is not None]
    if date is None:
        return snapshots[0] if snapshots else (None, None)
    named = resolve_snapshot(dest, date)
    for snapshot_dir, manifest in snapshots:
        if snapshot_dir == named:
            return snapshot_dir, manifest
    fail_unreadable(dest, date, named, config_path)


def snapshot_sources(manifest):
    """{source: {kind, tags, files, bytes}} for a manifest, keyed as the paths are on this machine.

    A repo contributes an untracked group and an ignored group over disjoint file sets, so the
    two sum into one source rather than competing for the key. Sources are remapped through this
    machine's home so a snapshot from the machine being replaced lines up with the config on the
    machine replacing it.
    """
    if manifest is None:
        return {}
    manifest_home = manifest.get('home')
    target_home = str(Path.home())
    sources = {}
    for group in manifest.get('groups', []):
        source = remap_home(group['source'], manifest_home, target_home)
        kind = 'path' if group['kind'] == 'path' else 'git repo'
        stored = sources.setdefault(source, {'kind': kind, 'tags': [], 'files': 0, 'bytes': 0})
        stored['tags'] += [tag for tag in group.get('tags', []) if tag not in stored['tags']]
        stored['files'] += group.get('files', 0)
        stored['bytes'] += group.get('bytes', 0)
    return sources


NOT_IN_CONFIG = 'not in the config'
TAGGED_IN_SNAPSHOT_ONLY = 'tagged in the snapshot only'
TAGGED_IN_CONFIG_ONLY = 'tagged in the config only'


def tag_index(config, sources):
    """{tag: [row]} over the config and a snapshot together.

    Either side alone hides one of the ways a tagged restore comes back empty: a tag added since
    the snapshot was taken selects nothing in it, and a tag renamed in the config is still the
    only name the snapshots taken before the rename answer to. A row's 'files' is None when the
    source is not in the snapshot at all. A source the snapshot holds without the tag is noted
    TAGGED_IN_CONFIG_ONLY, since a restore selects on the snapshot's tags and skips it.
    """
    index = {}
    entries = {str(path): (kind, tags) for kind, path, tags in config_entries(config)}

    for source in sorted(set(entries) | set(sources)):
        kind, config_tags = entries.get(source, (None, []))
        stored = sources.get(source)
        for tag in dict.fromkeys(config_tags + (stored['tags'] if stored else [])):
            if source not in entries:
                note = NOT_IN_CONFIG
            elif tag not in config_tags:
                note = TAGGED_IN_SNAPSHOT_ONLY
            elif stored and tag not in stored['tags']:
                note = TAGGED_IN_CONFIG_ONLY
            else:
                note = ''
            index.setdefault(tag, []).append(
                {
                    'kind': kind or stored['kind'],
                    'source': source,
                    'files': stored['files'] if stored else None,
                    'bytes': stored['bytes'] if stored else None,
                    'note': note,
                }
            )
    return index


def selected_by_tag(row):
    """Whether `restore --tag` brings this row back: the snapshot holds the source and tags it."""
    return row['files'] is not None and row['note'] != TAGGED_IN_CONFIG_ONLY


def sized_total(rows):
    """The files and bytes a restore by the tag brings back, from the rows it selects."""
    sized = [row for row in rows if selected_by_tag(row)]
    if not sized:
        return None
    return size_cell(sum(row['files'] for row in sized), sum(row['bytes'] for row in sized))


def size_cell(files, num_bytes):
    return f'{plural(files, "file"):>12}  {human_size(num_bytes):>9}'


def tilde(source):
    """A source with this machine's home abbreviated, since that prefix is on nearly every row."""
    home = str(Path.home())
    if source == home or source.startswith(home + '/'):
        return '~' + source[len(home) :]
    return source


def print_tag_sources(config_path, dest, snapshot_dir):
    """Name the two sides a tag listing is read from, since a tag can be on either alone."""
    config = f'config {cyan(config_handle(config_path))}'
    if snapshot_dir is None:
        print(f'  from {config}. No snapshots at {cyan(str(dest))} yet, so a restore by tag has nothing to read')
    else:
        print(f"  from {config} and snapshot {cyan(snapshot_dir.name)}. A restore by tag reads the snapshot's tags")


def resolve_tag_index(config, config_path, from_date: str | None):
    """The tag index, and the two things a listing has to name beside it.

    Both verbs read the config and one snapshot together, and both report which snapshot they
    sized against — so the resolution is here rather than duplicated in each.
    """
    dest = Path(config['back_up_to']).expanduser()
    snapshot_dir, manifest = snapshot_to_size_against(dest, from_date, config_path)
    return tag_index(config, snapshot_sources(manifest)), dest, snapshot_dir


def tag_record(name, rows, snapshot_dir):
    """One tag as --json reports it: its sources, sized against the snapshot named beside them."""
    return {
        'tag': name,
        'snapshot': snapshot_dir.name if snapshot_dir else None,
        'sources': [row | {'note': row['note'] or None} for row in rows],
    }


def show_tag_list(config, config_path, from_date: str | None, as_json: bool):
    """List the tags a restore can select on, and what each would bring back."""
    index, dest, snapshot_dir = resolve_tag_index(config, config_path, from_date)
    if as_json:
        print_json([tag_record(name, index[name], snapshot_dir) for name in sorted(index)])
        return

    print(f'{bold("safekeep:")} {plural(len(index), "tag")}')
    print_tag_sources(config_path, dest, snapshot_dir)

    if not index:
        print(f'\n  tag the sources and a restore can select them: {cyan(f"{safekeep_for(config_path)} config edit")}')
        return

    print()
    width = max(len(tag) for tag in index)
    for tag in sorted(index):
        rows = index[tag]
        # A tag that skips some of its sources still shows a size, and the size is the part that
        # reads as reassuring -- so the shortfall is named on the same row rather than left to be
        # noticed by drilling in. With no snapshot at all the header has said so already.
        skipped = len([row for row in rows if not selected_by_tag(row)])
        if snapshot_dir is None:
            sizes = ''
        elif skipped == len(rows):
            sizes = yellow('restores nothing from this snapshot')
        else:
            shortfall = f'  {yellow("skips " + plural(skipped, "source"))}' if skipped else ''
            sizes = f'{sized_total(rows)}{shortfall}'
        print(f'  {green(f"{tag:<{width}}")}  {plural(len(rows), "source"):<12}{sizes}'.rstrip())

    # Counted in the snapshot, because its tags are the ones a restore selects on.
    held = snapshot_sources(read_manifest(snapshot_dir)) if snapshot_dir else {}
    untagged = [source for source, stored in held.items() if not stored['tags']]
    if untagged:
        reach = f'only {cyan("--all")} or {cyan("--source")} restores them'
        print(f'\n  untagged in this snapshot: {plural(len(untagged), "source")}, so {reach}')
    print(f'\n  what one tag covers: {cyan(f"{safekeep_for(config_path)} tags show {shlex.quote(sorted(index)[0])}")}')


def show_tag(config, config_path, name: str, from_date: str | None, as_json: bool):
    """Show the sources one tag covers, and the restore that would bring them back."""
    index, dest, snapshot_dir = resolve_tag_index(config, config_path, from_date)
    rows = index.get(name)
    if not rows:
        snapshot = f' or snapshot {cyan(snapshot_dir.name)}' if snapshot_dir else ''
        print(f'{red("safekeep:")} no tag {yellow(name)} in config {cyan(config_handle(config_path))}{snapshot}', file=sys.stderr)
        if index:
            print(f'  tags: {green(", ".join(sorted(index)))}', file=sys.stderr)
        sys.exit(2)

    if as_json:
        print_json(tag_record(name, rows, snapshot_dir))
        return

    print(f'{bold("safekeep:")} tag {green(name)} covers {bold(plural(len(rows), "source"))}')
    print_tag_sources(config_path, dest, snapshot_dir)
    print()

    width = max(len(tilde(row['source'])) for row in rows)
    for row in rows:
        if snapshot_dir is None:
            sizes = ''
        elif row['files'] is None:
            sizes = yellow('absent from this snapshot')
        else:
            sizes = size_cell(row['files'], row['bytes'])
        note = f'  {yellow(row["note"])}' if row['note'] else ''
        print(f'  {row["kind"]:<9} {tilde(row["source"]):<{width}}  {sizes}{note}'.rstrip())

    selected = [row for row in rows if selected_by_tag(row)]
    # A total under a single row is the same number twice.
    if len(selected) > 1:
        print(f'  {"":<9} {"":<{width}}  {bold(sized_total(rows))}')
    if snapshot_dir is None:
        return

    # Pinned with --from, so the command restores the snapshot sized here even after a newer run.
    by_tag = rehearsal_request(tags=[name])
    if selected:
        print(f'\n  restore it: {cyan(restore_command(by_tag, config_path, from_snapshot=snapshot_dir.name))}')
        return
    # A run narrowed by --tag or --source leaves this snapshot without sources an older one tags.
    other = newest_snapshot_selecting(dest, by_tag, besides=snapshot_dir.name)
    if other:
        from_other = restore_command(by_tag, config_path, from_snapshot=other)
        instead = f'restore by the tag from the newest snapshot that carries it: {cyan(from_other)}'
    else:
        instead = f'take a snapshot that tags them: {cyan(f"{safekeep_for(config_path)} backup run")}'
    held = [row for row in rows if row['files'] is not None]
    if not held:
        print(f'\n  none of its sources are in {cyan(snapshot_dir.name)}')
        by_path = rehearsal_request(sources=[row['source'] for row in rows])
        holding = None if other else newest_snapshot_selecting(dest, by_path, besides=snapshot_dir.name)
        if holding:
            from_holding = restore_command(by_path, config_path, from_snapshot=holding)
            instead = f'restore them by path from the newest snapshot that holds them, untagged: {cyan(from_holding)}'
        print(f'  {instead}')
        return
    by_path = rehearsal_request(sources=[row['source'] for row in held])
    print(f'\n  {cyan(snapshot_dir.name)} holds these sources without the tag, so a restore by {green(name)} selects nothing in it.')
    print(f'  restore them by path: {cyan(restore_command(by_path, config_path, from_snapshot=snapshot_dir.name))}')
    print(f'  or {instead}')


def restorable_snapshots(dest, date, config_path):
    """Every snapshot with a manifest, newest first, or only the one `date` names."""
    if date is None:
        return [(d, m) for d, m in list_snapshots(dest) if m is not None]
    return [snapshot_to_size_against(dest, date, config_path)]


def snapshot_files(snapshot_dir, manifest, unreadable):
    """(path on this machine, path as recorded, stored copy) for every file one snapshot holds.

    Path sources record no file list in the manifest, so the snapshot's own tree is walked. The
    path is remapped through this machine's home, which is what lets a file from a WSL image with
    another username be recognized as the one this machine lacks. A directory the walk cannot
    list goes into `unreadable`.
    """
    home_then = manifest.get('home')
    home_now = str(Path.home())
    for row in source_rows(manifest.get('groups', [])):
        for origin, is_dir in stored_paths(snapshot_dir, row['source'], unreadable):
            if not is_dir:
                yield remap_home(str(origin), home_then, home_now), str(origin), snapshot_dir / snapshot_rel(origin)


def newest_copies(snapshots, progress):
    """({path on this machine: row}, unreadable) over every file the snapshots hold.

    The snapshots arrive newest first, so the first one a path turns up in holds the copy a
    restore would bring back. Every later sighting is an older copy of the same file.

    `unreadable` names each directory the walk could not list and each file it could not stat.
    The files beneath them are absent from the rows, so a listing that left them out of its
    verdict would call a partly read snapshot clean.
    """
    copies = {}
    unreadable = []
    read = 0
    for position, (snapshot_dir, manifest) in enumerate(snapshots, start=1):
        for path, recorded, stored in snapshot_files(snapshot_dir, manifest, unreadable):
            read += 1
            if progress and read % 200 == 0:
                status(f'reading snapshot {position} of {len(snapshots)} … {plural(read, "file")}')
            if path in copies:
                continue
            try:
                size = stored.stat().st_size
            except OSError:
                unreadable.append(str(stored))
                continue
            copies[path] = {
                'path': path,
                'recorded': recorded,
                'here': os.path.lexists(path),
                'snapshot': snapshot_dir.name,
                'label': manifest.get('label'),
                'host': manifest.get('hostname'),
                'bytes': size,
                'stored': str(stored),
            }
    if progress:
        clear_status()
    return copies, unreadable


def report_unreadable(unreadable):
    """Name what a listing could not read, and fail the run, since its rows are incomplete."""
    sys.stdout.flush()
    print(f'\n{red("safekeep:")} {plural(len(unreadable), "path")} in the snapshots could not be read:', file=sys.stderr)
    for path in unreadable[:10]:
        print(f'  {path}', file=sys.stderr)
    if len(unreadable) > 10:
        print(f'  ... and {len(unreadable) - 10} more', file=sys.stderr)
    print(f'  anything {"it holds" if len(unreadable) == 1 else "they hold"} is missing from this listing', file=sys.stderr)
    sys.exit(1)


def shell_path(path):
    """A path as it can be pasted into a command: abbreviated, unless that would need quoting.

    Quoting a tilde stops the shell expanding it, so a path that needs quotes is printed whole.
    shlex counts the tilde itself as unsafe, which is why only what follows it is checked.
    """
    short = tilde(path)
    if short != path and shlex.quote(short[1:]) == short[1:]:
        return short
    return shlex.quote(path)


def config_handle(config_path):
    """What -c takes to find this config: its name in the config directory, or else its path."""
    path = Path(config_path)
    return shlex.quote(path.stem) if path.parent == CONFIG_DIR else shell_path(str(path))


def safekeep_for(config_path):
    """`safekeep` as a printed command must start to read this config again.

    Bare when it is the only config, since -c is then implied. Otherwise with the -c that finds it.
    """
    path = Path(config_path)
    if path.parent == CONFIG_DIR and config_names() == [path.stem]:
        return 'safekeep'
    return f'safekeep -c {config_handle(path)}'


def show_files(config, config_path, missing: bool, from_date: str | None, as_json: bool):
    """Every file the snapshots hold, one line each, grouped under the newest snapshot holding it."""
    dest = Path(config['back_up_to']).expanduser()
    snapshots = restorable_snapshots(dest, from_date, config_path)
    copies, unreadable = newest_copies(snapshots, progress=not as_json)
    rows = sorted(copies.values(), key=lambda row: row['path'])
    absent = [row for row in rows if not row['here']]
    if missing:
        rows = absent

    if as_json:
        print_json(rows)
        if unreadable:
            report_unreadable(unreadable)
        return

    if not snapshots:
        print(f'{yellow("safekeep:")} no restorable snapshots at {cyan(str(dest))}')
        print(f'  take one: {cyan(f"{safekeep_for(config_path)} backup run")}')
        return

    read_from = f'snapshot {cyan(snapshots[0][0].name)}' if from_date else f'{plural(len(snapshots), "snapshot")}'
    partly = f', {red(f"{plural(len(unreadable), 'path')} unreadable")}' if unreadable else ''
    if missing and not rows:
        every = 'every file it could read' if unreadable else 'every file'
        print(f'{bold("safekeep:")} {every} in {read_from} is on this machine{partly}')
    elif missing:
        verb = 'is' if len(rows) == 1 else 'are'
        print(f'{bold("safekeep:")} {bold(plural(len(rows), "file"))} in {read_from} {verb} not on this machine{partly}')
    else:
        shortfall = f', {yellow(f"{len(absent)} not on this machine")}' if absent else ', every one on this machine'
        print(f'{bold("safekeep:")} {bold(plural(len(rows), "file"))} in {read_from}{shortfall}{partly}')
    print(f'  at {cyan(str(dest))}' if from_date else f'  at {cyan(str(dest))}, each from the newest snapshot holding it')

    # Grouped under the snapshot rather than repeating it on every row: the snapshot is what a
    # restore names with --from, and its label is what says which machine the files came off.
    for snapshot_dir, manifest in snapshots:
        held = [row for row in rows if row['snapshot'] == snapshot_dir.name]
        if not held:
            continue
        heading = f'{snapshot_dir.name}  {manifest.get("hostname", "?")}'
        label = f'  {green(clip_to_terminal(manifest["label"], len(heading) + 4))}' if manifest.get('label') else ''
        print(f'\n  {bold(snapshot_dir.name)}  {cyan(manifest.get("hostname", "?"))}{label}')
        for row in held:
            note = '' if row['here'] or missing else f'  {yellow("not on this machine")}'
            print(f'    {human_size(row["bytes"]):>9}  {tilde(row["path"])}{note}')

    if absent:
        first = absent[0]
        restore = f'{safekeep_for(config_path)} restore --to / --from {first["snapshot"]} --source {shell_path(first["path"])}'
        print(f'\n  restore one: {cyan(restore)}')
    if unreadable:
        report_unreadable(unreadable)


def require_fzf():
    if shutil.which('fzf'):
        return
    print(f'{red("safekeep:")} fzf is required for interactive selection', file=sys.stderr)
    print(f'  select non-interactively instead: {cyan("--all")}, {cyan("--source PATH")}, or {cyan("--tag NAME")}', file=sys.stderr)
    sys.exit(1)


def fzf_cell(text):
    """Free text flattened to one tab-free field.

    A picker row is split on tabs, so a label holding one would shift every field after it and
    a selection would be read out of the wrong column. Newlines would split the row outright.
    """
    return ' '.join(text.split()) if text else ''


def fzf(lines, args):
    """Run fzf over lines, returning the selected ones."""
    result = subprocess.run(['fzf', *args], input='\n'.join(lines), capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def preview_safekeep(config_path):
    """The safekeep an fzf preview pane runs, reading the config this run resolved.

    `-m safekeep` rather than this file's path: as a package, __file__ is
    src/safekeep/__init__.py, and running that directly re-imports the module
    under the name __main__ instead of resolving the installed package. The config goes by
    its path, because a config passed as a file has no name that resolves to it.
    """
    return shlex.join([sys.executable, '-m', 'safekeep', '-c', str(config_path)])


def pick_snapshot(dest, config_path):
    """Interactively choose a snapshot, previewing each one's manifest."""
    snapshots = [(d, m) for d, m in list_snapshots(dest) if m is not None]
    if not snapshots:
        print(f'{red("safekeep:")} no restorable snapshots at {cyan(str(dest))}', file=sys.stderr)
        sys.exit(1)

    preview_cmd = f'{preview_safekeep(config_path)} snapshots show {{2}}'

    # One preformatted column block with the raw name hidden behind it, the same arrangement
    # pick_sources uses: fzf renders a tab as a tab stop rather than aligning a column, and a
    # destination holds names of two widths, so the shorter rows would step left.
    width = snapshot_name_width(snapshots)
    lines = []
    for snapshot_dir, manifest in snapshots:
        groups = manifest.get('groups', [])
        total_bytes = sum(g.get('bytes', 0) for g in groups)
        shown = f'{snapshot_dir.name:<{width}}  {human_size(total_bytes):>9}  {plural(len(source_rows(groups)), "source"):<10}'
        lines.append(f'{shown}  {fzf_cell(manifest.get("label"))}'.rstrip() + f'\t{snapshot_dir.name}')

    selected = fzf(
        lines,
        [
            '--delimiter=\t',
            '--with-nth=1',
            '--header=select a snapshot   ↑↓ move · enter choose · esc cancel',
            '--header-first',
            '--preview',
            preview_cmd,
            '--preview-window=right:60%',
        ],
    )
    if not selected:
        return None
    # Field 2, not the padded block in field 1 -- the name is what everything downstream opens.
    return selected[0].split('\t')[1]


def pick_sources(snapshot_dir, manifest, config_path):
    """Interactively choose sources, previewing the files the snapshot holds for each."""
    rows = source_rows(manifest.get('groups', []))
    if not rows:
        return []

    # One preformatted column block, with the raw source hidden in a second field: a padded
    # column cannot double as a path, and fzf's own tab rendering will not align these.
    width = max(len(row['source']) for row in rows)
    kind_width = max(len(kinds_label(row['kinds'])) for row in rows)
    lines = [
        f'{row["source"]:<{width}}  {kinds_label(row["kinds"]):<{kind_width}}  '
        f'{size_cell(row["files"], row["bytes"])}  {",".join(row["tags"])}'.rstrip()
        + f'\t{row["source"]}'
        for row in rows
    ]

    preview_cmd = f'{preview_safekeep(config_path)} snapshots show {snapshot_dir.name} --source {{2}}'
    selected = fzf(
        lines,
        [
            '--multi',
            '--delimiter=\t',
            '--with-nth=1',
            # fzf binds ctrl-a to beginning-of-line and leaves select-all unbound.
            '--bind=ctrl-a:select-all',
            # Named in full and pinned above the prompt, since the picker is the one place the
            # multi-select keys can be recalled at the moment they are needed.
            '--header=tab select · shift-tab deselect · ctrl-a select all · enter restore · esc cancel',
            '--header-first',
            '--preview',
            preview_cmd,
            '--preview-window=right:60%',
        ],
    )

    chosen = {line.split('\t')[1] for line in selected if '\t' in line}
    return [row for row in rows if row['source'] in chosen]


PREVIEW_FILE_LIMIT = 200


def show_snapshot_source_files(dest, date, source, config_path, as_json=False):
    """The files a snapshot holds for one source — also the fzf preview pane."""
    snapshot_dir = resolve_snapshot(dest, date)
    manifest = read_manifest(snapshot_dir) if snapshot_dir else None
    if manifest is None:
        fail_unreadable(dest, date, snapshot_dir, config_path)

    home_then = manifest.get('home')
    home_now = str(Path.home())
    wanted = normalized_needle(source)
    rows = source_rows(manifest.get('groups', []))
    row = next((row for row in rows if wanted in source_names(row['source'], home_then, home_now)), None)
    if row is None:
        held = ', '.join(tilde(remap_home(row['source'], home_then, home_now)) for row in rows)
        print(f'{red("safekeep:")} {source} is not a source in {snapshot_dir.name}\n  its sources: {held}', file=sys.stderr)
        sys.exit(1)
    source = row['source']

    kinds = file_kinds(manifest)
    if as_json:
        # Every file rather than the preview's first PREVIEW_FILE_LIMIT: the cap is for a pane.
        files = [origin for origin, is_dir in stored_paths(snapshot_dir, source) if not is_dir]
        print_json(
            [
                {
                    'path': remap_home(str(origin), home_then, home_now),
                    'recorded': str(origin),
                    'kind': kinds.get(snapshot_rel(origin)),
                }
                for origin in files
            ]
        )
        return
    print(f'{kinds_label(row["kinds"])}   {plural(row["files"], "file")}   {human_size(row["bytes"])}')
    if row['tags']:
        print(f'tags: {", ".join(row["tags"])}')
    print()

    files = [origin for origin, is_dir in stored_paths(snapshot_dir, source) if not is_dir]
    for origin in files[:PREVIEW_FILE_LIMIT]:
        label = kinds.get(snapshot_rel(origin), '')
        relative = origin.name if str(origin) == source else os.path.relpath(str(origin), source)
        print(f'  {relative}{f"   {label}" if label else ""}')
    if len(files) > PREVIEW_FILE_LIMIT:
        print(f'  ... and {len(files) - PREVIEW_FILE_LIMIT} more')


def select_groups(manifest, request: RestoreRequest):
    """Resolve which groups to restore from flags, or None if selection is interactive.

    A --source is matched against each source both as recorded and as this machine names it, so
    a path copied out of `files list` still matches a snapshot taken under another home.
    """
    groups = manifest.get('groups', [])
    if request.all:
        return groups

    if not request.source and not request.tag:
        return None

    home_then = manifest.get('home')
    home_now = str(Path.home())
    needles = [normalized_needle(needle) for needle in request.source]
    selected = []
    for group in groups:
        names = source_names(group['source'], home_then, home_now)
        matched_source = any(needle in name for needle in needles for name in names)
        matched_tag = any(tag in group.get('tags', []) for tag in request.tag)
        if matched_source or matched_tag:
            selected.append(group)
    return selected


def rows_inside_sources(snapshot_dir, manifest, needles):
    """A row for each --source naming a file or directory inside a source, covering only that path.

    The needle is a path as this machine names it, which is how `files list` prints one, or as
    the snapshot recorded it, which is how `snapshots show` prints one. Either is mapped onto the
    recorded source before it is looked up. 'within' is the source it sits in: the restore creates
    the directories between the two, and gives them the modes it recorded.
    """
    home_then = manifest.get('home')
    home_now = str(Path.home())
    rows = {}
    for needle in needles:
        wanted = normalized_needle(needle)
        if not os.path.isabs(wanted):
            continue
        for group in manifest.get('groups', []):
            source = group['source']
            named = next(
                (name for name in source_names(source, home_then, home_now) if wanted.startswith(name.rstrip('/') + '/')),
                None,
            )
            if named is None:
                continue
            origin = os.path.normpath(source) + wanted[len(named) :]
            if not (snapshot_dir / snapshot_rel(origin)).exists():
                continue
            row = rows.setdefault(origin, {'source': origin, 'within': source, 'kinds': [], 'tags': []})
            # Sources can nest, a path entry around a repo, and the outermost reaches every
            # directory the restore might have to create.
            if len(source) < len(row['within']):
                row['within'] = source
            # A repo's two groups share one subtree, and the file lists say which kind is here.
            rel = snapshot_rel(origin)
            listed = group.get('paths')
            holds_it = listed is None or any(path == rel or path.startswith(rel + '/') for path in listed)
            if holds_it and group['kind'] not in row['kinds']:
                row['kinds'].append(group['kind'])
            row['tags'] += [tag for tag in group.get('tags', []) if tag not in row['tags']]

    for row in rows.values():
        stored = [snapshot_dir / snapshot_rel(origin) for origin, is_dir in stored_paths(snapshot_dir, row['source']) if not is_dir]
        row['files'] = len(stored)
        row['bytes'] = sum(path.stat().st_size for path in stored)
    return list(rows.values())


def remap_home(source, manifest_home, target_home):
    """Rewrite a source path recorded under the backup machine's home into this one's."""
    if manifest_home in (None, target_home):
        return source
    if source == manifest_home:
        return target_home
    if source.startswith(manifest_home + '/'):
        return target_home + source[len(manifest_home) :]
    return source


def normalized_needle(needle):
    """A --source as typed, with its ~ expanded and any trailing slash or ./ dropped."""
    return os.path.normpath(os.path.expanduser(needle))


def source_names(source, manifest_home, target_home):
    """A recorded source as the snapshot names it, then as this machine names it."""
    return (os.path.normpath(source), os.path.normpath(remap_home(source, manifest_home, target_home)))


def paths_under(source, candidates):
    """Absolute paths from candidates that are the source itself or live beneath it."""
    prefix = str(source).rstrip('/') + '/'
    return [p for p in candidates if p == str(source) or p.startswith(prefix)]


def paths_under_any(sources, candidates):
    """Absolute paths from candidates covered by any of the sources, deduplicated."""
    return sorted({p for source in sources for p in paths_under(source, candidates)})


def symlinked_ancestors(row, candidates):
    """The directories above a path inside a source that were symlinks when backed up.

    Only a row naming a path inside a source has any. A symlink at ~/.config/nvim is
    dereferenced into the snapshot, so restoring ~/.config/nvim/init.lua writes through that
    link where it still exists here, and into a fresh real directory where it does not.
    """
    within = row.get('within')
    if within is None:
        return []
    links = set(candidates)
    return sorted(str(parent) for parent in Path(row['source']).parents if parent.is_relative_to(within) and str(parent) in links)


def selection_count(rows):
    """How many whole sources and how many paths inside one a restore covers, in words."""
    inside = [row for row in rows if row.get('within')]
    whole = len(rows) - len(inside)
    parts = [plural(whole, 'source')] if whole or not inside else []
    if inside:
        holders = 'a source' if len({row['within'] for row in inside}) == 1 else 'sources'
        parts.append(f'{plural(len(inside), "path")} inside {holders}')
    return ' and '.join(parts)


class RestoreAborted(Exception):
    """Answering quit at an --on-conflict ask prompt, which stops the run where it stands."""


def stored_paths(snapshot_dir, source, unreadable=None):
    """Every path the snapshot holds for one source, as the absolute paths it had when backed up.

    Parents come before children, and the source itself is the first entry.

    This list is what the restore reports, compares against the target, and reapplies modes to.
    The target's own tree can answer none of those: a repo whose untracked files are being
    restored has a whole working tree beside them that no snapshot ever recorded, and walking
    that is how a restore of two hundred files came to chmod eleven thousand paths.

    Each directory the walk cannot list is appended to `unreadable` when a caller passes one.
    """
    stored = snapshot_dir / snapshot_rel(source)
    if stored.is_file():
        return [(Path(source), False)]

    def note(error):
        if unreadable is not None:
            unreadable.append(error.filename)

    found = [(Path(source), True)]
    for dirpath, dirnames, filenames in os.walk(stored, onerror=note):
        relative = os.path.relpath(dirpath, stored)
        base = Path(source) if relative == '.' else Path(source) / relative
        found.extend((base / name, True) for name in sorted(dirnames))
        found.extend((base / name, False) for name in sorted(filenames))
    return found


def target_entries(snapshot_dir, source, target_root, manifest_home, target_home, skip, within=None):
    """Each path the snapshot holds for a source: its name, where it lands, whether it is there.

    'name' is the path relative to the source, which is both what rsync reports a transfer under
    and the shortest thing that still identifies a file beneath a line that named the source.

    'existed' is read before rsync runs and is the only chance to read it: afterwards every path
    is present and nothing distinguishes the file that was overwritten from the one that was
    created.

    `within` is the source that `source` sits inside, when a restore names a path in one. The
    directories between the two that the target lacks are entries too, because restoring one
    file into a fresh ~/.ssh is exactly when that directory's 0700 has to be put back.
    """
    from_a_file = (snapshot_dir / snapshot_rel(source)).is_file()
    entries = []
    if within is not None:
        for parent in Path(source).parents:
            target = Path(target_root) / snapshot_rel(remap_home(str(parent), manifest_home, target_home))
            if parent.is_relative_to(within) and not target.exists():
                entries.append({'name': str(parent), 'origin': parent, 'is_dir': True, 'target': target, 'existed': False})
    for origin, is_dir in stored_paths(snapshot_dir, source):
        if str(origin) in skip:
            continue
        target = Path(target_root) / snapshot_rel(remap_home(str(origin), manifest_home, target_home))
        name = origin.name if from_a_file else os.path.relpath(str(origin), source)
        entries.append({'name': name, 'origin': origin, 'is_dir': is_dir, 'target': target, 'existed': target.exists()})
    return entries


def read_answer(prompt, valid, default):
    """Read one keystroke's worth of answer, re-asking until it is one of the offered ones."""
    while True:
        try:
            answer = input(prompt).strip().lower()[:1]
        except EOFError as error:
            raise RestoreAborted from error
        if not answer:
            return default
        if answer in valid:
            return answer


def ask_about_conflicts(conflicts, decision):
    """Ask about each file already at the target, returning the origins to leave alone.

    Only conflicts are asked about: a file the target does not have is not a decision, and a
    restore that asked about every file would be unanswerable at the size it is run at. The
    answer to keep is the default, because the prompt is answered fastest by whoever is least
    sure, and 'keep' is the one that cannot lose anything.
    """
    declined = set()
    for entry in conflicts:
        if decision['all'] is None:
            answer = read_answer(
                f'      {yellow("~")} {entry["name"]} exists — overwrite? [y]es [N]o [a]ll [k]eep all [q]uit: ', 'ynakq', 'n'
            )
            if answer == 'q':
                raise RestoreAborted
            if answer == 'a':
                decision['all'] = True
            elif answer == 'k':
                decision['all'] = False
            elif answer == 'n':
                declined.add(str(entry['origin']))
                continue
            else:
                continue
        if not decision['all']:
            declined.add(str(entry['origin']))
    return declined


def restore_file_line(name, entry, kind, on_conflict: ConflictPolicy):
    """One restored file, said in terms of what it did to the target."""
    label = f'  {cyan(kind)}' if kind else ''
    if entry is None or not entry['existed']:
        return f'{green("+")} {name}{label}'
    kept = f'  {yellow(f"kept {PRE_RESTORE_SUFFIX} copy")}' if on_conflict == ConflictPolicy.BACKUP else ''
    return f'{yellow("~")} {name}{label}{kept}'


def restore_source(snapshot_dir, row, target_root, manifest_home, target_home, symlinks, kinds, request: RestoreRequest, decision):
    """Rsync one source's subtree out of the snapshot, naming each file as rsync writes it.

    Returns the per-path records the mode pass needs, or None when the source was skipped.
    """
    source = row['source']
    stored = snapshot_dir / snapshot_rel(source)
    if not stored.exists():
        print(f'      {yellow("skip: not present in snapshot")}')
        return None

    symlinked = paths_under(source, ['/' + rel for rel in symlinks])
    if request.skip_symlinked and str(source) in symlinked:
        print(f'      {yellow("skip: was a symlink")}')
        return None
    linked_above = symlinked_ancestors(row, ['/' + rel for rel in symlinks])
    if request.skip_symlinked and linked_above:
        print(f'      {yellow(f"skip: inside {tilde(linked_above[0])}, which was a symlink")}')
        return None

    entries = target_entries(
        snapshot_dir,
        source,
        target_root,
        manifest_home,
        target_home,
        set(symlinked) if request.skip_symlinked else set(),
        within=row.get('within'),
    )
    conflicts = [entry for entry in entries if not entry['is_dir'] and entry['existed']]

    declined = ask_about_conflicts(conflicts, decision) if request.on_conflict == ConflictPolicy.ASK and conflicts else set()
    entries = [entry for entry in entries if str(entry['origin']) not in declined]
    files = [entry for entry in entries if not entry['is_dir']]
    if not files:
        print(f'      {yellow("kept every file")} — {plural(len(declined), "file")} declined')
        return entries

    target = Path(target_root) / snapshot_rel(remap_home(source, manifest_home, target_home))
    by_name = {entry['name']: entry for entry in files}
    transferred = []

    def report(name):
        entry = by_name.get(name)
        transferred.append(entry)
        kind = kinds.get(snapshot_rel(entry['origin'])) if entry else ''
        print(f'      {restore_file_line(name, entry, kind, request.on_conflict)}')

    cmd = ['rsync', '-a']
    if request.on_conflict == ConflictPolicy.SKIP:
        cmd.append('--ignore-existing')
    elif request.on_conflict == ConflictPolicy.NEWER:
        cmd.append('--update')
    else:
        # backup, overwrite and an answered ask all mean the snapshot wins, so the quick check
        # has to go. rsync skips a file with the same size and mtime without ever reading it,
        # which is right for a sync and wrong for a restore: a file corrupted in place keeps
        # both its size and its timestamp, and that is precisely the file being restored over.
        # Checksum rather than --ignore-times, so genuinely identical files are still skipped
        # and no .pre-restore copy is manufactured for a file that never changed.
        cmd.append('--checksum')
        if request.on_conflict == ConflictPolicy.BACKUP:
            cmd.extend(['--backup', f'--suffix={PRE_RESTORE_SUFFIX}'])

    if request.skip_symlinked and stored.is_dir():
        for abs_path in symlinked:
            cmd.extend(['--exclude', '/' + os.path.relpath(abs_path, str(source))])

    if request.dry_run:
        cmd.append('-n')
    cmd.extend(rsync_naming_flags())

    listing = None
    if declined and stored.is_dir():
        # An exclude is a glob, and a filename holding a bracket or an asterisk would be matched
        # as a pattern rather than as itself. --files-from names the survivors literally.
        listing = write_path_list(sorted(by_name))
        cmd.extend(['--files-from', listing])

    if stored.is_dir():
        cmd.extend([str(stored) + '/', str(target) + '/'])
    else:
        cmd.extend([str(stored), str(target)])

    if not request.dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
    elif not target.parent.exists():
        # A rehearsal into a fresh directory, which is the documented way to use --dry-run. The
        # parent cannot be created here without writing, and rsync will not accept a file
        # destination whose directory is missing -- it exits 3 rather than reporting a transfer.
        # Nothing exists to conflict with, so every file under the source would be created and
        # rsync has no question left to answer that this list does not.
        for name in sorted(by_name):
            report(name)
        report_source_totals(transferred, files, declined, request.dry_run)
        return entries

    try:
        run_rsync_naming_files(cmd, report)
    finally:
        if listing:
            os.unlink(listing)

    report_source_totals(transferred, files, declined, request.dry_run)
    return entries


def write_path_list(names):
    """Write a newline-delimited path list for rsync --files-from, returning its path."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as tmp:
        tmp.write('\n'.join(names) + '\n')
        return tmp.name


def report_source_totals(transferred, files, declined, dry_run):
    """What the source's rsync did, in the four outcomes a file can have.

    Unchanged is the one worth printing even when it is everything: a restore that names no
    files is otherwise indistinguishable from one that failed to find any.
    """
    written = [entry for entry in transferred if entry is not None]
    created = sum(1 for entry in written if not entry['existed'])
    restored, replaced = ('would be restored', 'would be replaced') if dry_run else ('restored', 'replaced')
    parts = []
    if created:
        parts.append(green(f'{plural(created, "file")} {restored}'))
    if len(written) - created:
        parts.append(yellow(f'{len(written) - created} {replaced}'))
    if declined:
        parts.append(f'{len(declined)} kept')
    if len(files) - len(written):
        parts.append(f'{len(files) - len(written)} unchanged')
    print(f'      {" · ".join(parts)}')


def apply_modes(manifest, entries, dry_run):
    """Reapply the source modes the destination filesystem could not store.

    The recorded deviation is applied wherever the manifest has one; the default is applied
    only to paths this restore created. A path that was already at the target and has no
    recorded mode is left alone — nothing about it was ever in the snapshot, so the default
    would be a guess, and the guess strips the executable bit off files the restore never
    touched. That is what chmodded a whole git working tree when this walked the target.

    Files before directories, deepest first, so a directory restored to 0500 cannot lock the
    pass out of the paths beneath it.
    """
    modes = manifest.get('modes', {})
    ordered = sorted(entries, key=lambda entry: (entry['is_dir'], -len(entry['origin'].parts)))
    changed = 0
    recorded = 0

    for entry in ordered:
        wanted = modes.get(snapshot_rel(entry['origin']))
        if wanted is None and entry['existed']:
            continue
        if wanted is not None:
            recorded += 1
        if dry_run:
            changed += 1
            continue
        try:
            entry['target'].chmod(int(wanted, 8) if wanted else (DEFAULT_DIR_MODE if entry['is_dir'] else DEFAULT_FILE_MODE))
        except OSError:
            continue
        changed += 1
        # One syscall per path, and over SMB that is minutes of silence on a large source.
        # The interval is a redraw budget rather than a reporting granularity.
        if changed % 200 == 0:
            status(f'reapplying modes … {plural(changed, "path")}')

    clear_status()
    return changed, recorded


def restore_command(request: RestoreRequest, config_path, from_snapshot=None):
    """The restore `request` describes, as a command line that runs as printed."""
    head = safekeep_for(config_path) + (' --no-input' if request.no_input else '') + ' restore'
    # Each word is quoted alone, because shlex.join would quote a tilde and a quoted one never expands.
    snapshot = from_snapshot or request.from_date
    words = ['--to', shell_path(request.to)] + (['--from', shlex.quote(snapshot)] if snapshot else [])
    selection = (
        (['--all'] if request.all else [])
        + [w for s in request.source for w in ('--source', shell_path(s))]
        + [w for t in request.tag for w in ('--tag', shlex.quote(t))]
    )
    words += selection or ['--all']
    if request.on_conflict != ConflictPolicy.BACKUP:
        words += ['--on-conflict', request.on_conflict.value]
    if request.skip_symlinked:
        words.append('--skip-symlinked')
    if request.dry_run:
        words.append('-n')
    return ' '.join([head, *words])


def rehearsal_request(tags=(), sources=()):
    """A restore into the rehearsal directory, for a command printed beside a listing."""
    return RestoreRequest(
        to=str(REHEARSAL_ROOT),
        from_date=None,
        all=False,
        source=list(sources),
        tag=list(tags),
        on_conflict=ConflictPolicy.BACKUP,
        skip_symlinked=False,
        dry_run=False,
        no_input=False,
    )


def selected_rows(snapshot_dir, manifest, request: RestoreRequest):
    """The sources an explicit selection takes from one snapshot, and the paths it names inside them."""
    rows = source_rows(select_groups(manifest, request) or [])
    if request.all:
        return rows
    whole = {row['source'] for row in rows}
    inside = rows_inside_sources(snapshot_dir, manifest, request.source)
    return sorted(rows + [row for row in inside if row['source'] not in whole], key=lambda row: row['source'])


def newest_snapshot_selecting(dest, request: RestoreRequest, besides):
    """The newest snapshot other than `besides` that the selection restores anything from."""
    for snapshot_dir, manifest in list_snapshots(dest):
        if manifest is not None and snapshot_dir.name != besides and selected_rows(snapshot_dir, manifest, request):
            return snapshot_dir.name
    return None


def explain_empty_selection(dest, manifest, date, request: RestoreRequest, config_path):
    """Say why an explicit selection matched nothing in this snapshot, and which snapshot it would match.

    Tags live in the manifest, not in the config -- each one is a copy of what the config said
    on the day the snapshot was taken. Tagging an entry today does not retag the snapshots that
    already exist, and that is the whole of why a restore comes back empty while the config
    plainly carries the tag. A run narrowed by --tag or --source leaves the newest snapshot
    holding only part of what the one before it holds, which is the other way to arrive here.
    """
    groups = manifest.get('groups', [])
    if request.tag:
        available = sorted({tag for group in groups for tag in group.get('tags', [])})
        compare = f'{safekeep_for(config_path)} tags list --from {date}'
        print(f'  no source in {cyan(date)} carries {yellow(", ".join(request.tag))}', file=sys.stderr)
        print(f'  tags in this snapshot: {green(", ".join(available)) if available else yellow("none")}', file=sys.stderr)
        print(f'  a snapshot carries the tags its config had that day, compared with the config by: {cyan(compare)}', file=sys.stderr)
    if request.source:
        needles = yellow(', '.join(request.source))
        home_then, home_now = manifest.get('home'), str(Path.home())
        print(f'  no source in {cyan(date)} contains {needles}, and it holds nothing at that path inside one:', file=sys.stderr)
        for row in source_rows(groups):
            print(f'    {tilde(remap_home(row["source"], home_then, home_now))}', file=sys.stderr)
    if request.all and not groups:
        print(f'  {cyan(date)} records nothing at all', file=sys.stderr)
    other = newest_snapshot_selecting(dest, request, besides=date)
    if other is not None:
        print(
            f'  restore it from the newest snapshot that holds it: {cyan(restore_command(request, config_path, from_snapshot=other))}',
            file=sys.stderr,
        )


def can_prompt(no_input: bool):
    """Whether a question may be asked: --no-input never allows one, and otherwise
    stdin has to be a terminal.

    A prompt on a stdin that never closes leaves the caller with no output and no
    exit code, so every interactive path in a restore asks this first."""
    return not no_input and sys.stdin.isatty()


def do_restore(config, config_path, request: RestoreRequest):
    dest = Path(config['back_up_to']).expanduser()

    if request.on_conflict == ConflictPolicy.ASK and not can_prompt(request.no_input):
        print(f'{red("safekeep:")} {cyan("--on-conflict ask")} has to be answered, and this run cannot ask', file=sys.stderr)
        policies = ', '.join(cyan(policy) for policy in ConflictPolicy if policy != ConflictPolicy.ASK)
        print(f'  decide up front instead: {policies} — {cyan("backup")} is the default', file=sys.stderr)
        sys.exit(2)

    if request.from_date:
        date = request.from_date
    elif can_prompt(request.no_input) and not (request.all or request.source or request.tag):
        require_fzf()
        date = pick_snapshot(dest, config_path)
        if date is None:
            print(f'{yellow("safekeep:")} nothing selected, nothing restored')
            return
    else:
        restorable = [d for d, m in list_snapshots(dest) if m is not None]
        if not restorable:
            print(f'{red("safekeep:")} no restorable snapshots at {cyan(str(dest))}', file=sys.stderr)
            print(f'  take one: {cyan(f"{safekeep_for(config_path)} backup run")}', file=sys.stderr)
            sys.exit(1)
        date = restorable[0].name

    snapshot_dir = resolve_snapshot(dest, date)
    manifest = read_manifest(snapshot_dir) if snapshot_dir else None
    if manifest is None:
        print(f'{red("safekeep:")} {unreadable_reason(dest, date, snapshot_dir, config_path)}', file=sys.stderr)
        if snapshot_dir is not None:
            rsync = f'rsync -av {shell_path(str(snapshot_dir))}/ {shell_path(request.to)}'
            print(f'  copy it out with rsync, without the modes a manifest would restore: {cyan(rsync)}', file=sys.stderr)
        sys.exit(1)
    # A --from naming a day resolves to the last run of it, so the rest of this reports the name
    # that was resolved rather than the one that was typed.
    date = snapshot_dir.name

    groups = select_groups(manifest, request)
    if groups is None:
        if not can_prompt(request.no_input):
            print(f'{red("safekeep:")} which sources from {cyan(date)}? This run cannot ask, so name them', file=sys.stderr)
            print(f'  with {cyan("--all")}, {cyan("--source PATH")} or {cyan("--tag NAME")}. The snapshot holds:', file=sys.stderr)
            home_then, home_now = manifest.get('home'), str(Path.home())
            for row in source_rows(manifest.get('groups', [])):
                source = tilde(remap_home(row['source'], home_then, home_now))
                tags = f'  {green(" ".join(row["tags"]))}' if row['tags'] else ''
                print(f'  {kinds_label(row["kinds"]):<20} {source}{tags}', file=sys.stderr)
            sys.exit(1)
        require_fzf()
        rows = pick_sources(snapshot_dir, manifest, config_path)
    else:
        rows = selected_rows(snapshot_dir, manifest, request)

    if not rows:
        # An explicit selection that matched nothing is a failed request, not a canceled one:
        # exit non-zero so a caller cannot read it as a restore that happened to be empty.
        if request.all or request.source or request.tag:
            print(f'{red("safekeep:")} nothing selected, nothing restored', file=sys.stderr)
            explain_empty_selection(dest, manifest, date, request, config_path)
            sys.exit(1)
        print(f'{yellow("safekeep:")} nothing selected, nothing restored')
        return

    manifest_home = manifest.get('home')
    target_home = str(Path.home())
    symlinks = manifest.get('symlinks', {})
    kinds = file_kinds(manifest)

    verb = yellow('would restore') if request.dry_run else green('restoring')
    print(f'{bold("safekeep:")} {verb} {bold(selection_count(rows))} from {cyan(date)} to {cyan(request.to)}')
    # A date says when a snapshot was taken and nothing about why, which is the question being
    # answered when an older one is picked on purpose.
    if manifest.get('label'):
        print(f'  labeled {green(manifest["label"])}')
    if manifest_home and manifest_home != target_home:
        print(f'  remapping {cyan(manifest_home)} -> {cyan(target_home)}')
    if request.on_conflict in (ConflictPolicy.BACKUP, ConflictPolicy.OVERWRITE, ConflictPolicy.ASK) and not request.dry_run:
        # Named because it is the whole reason a restore takes as long as it does, and an
        # unexplained wait reads as a hang. The other two modes skip on mtime and are quick.
        print(f'  comparing by {cyan("checksum")}, which reads every file on both sides')
    print()

    restored = []
    entries = []
    decision = {'all': None}
    # Where each source lands rather than where it was recorded, which is the form --source and
    # `files list` both take.
    landing = {row['source']: tilde(remap_home(row['source'], manifest_home, target_home)) for row in rows}
    width = max(len(name) for name in landing.values())
    for position, row in enumerate(rows, start=1):
        # Printed before the work rather than after it: this line is what says which source the
        # wait belongs to, and after the fact it says nothing that was not already known. The
        # kinds are on it because a repo's path alone reads as though the repo is being
        # restored, when what a snapshot holds is the files git could not put back.
        counter = cyan(f'[{position}/{len(rows)}]')
        sizes = size_cell(row['files'], row['bytes'])
        print(f'  {counter} {landing[row["source"]]:<{width}}  {sizes}  {cyan(kinds_label(row["kinds"]))}', flush=True)
        try:
            restored_entries = restore_source(snapshot_dir, row, request.to, manifest_home, target_home, symlinks, kinds, request, decision)
        except RestoreAborted:
            print(f'\n  {yellow("stopped here")} — the sources already restored are left as they are')
            break
        if restored_entries is not None:
            restored.append(row)
            entries.extend(restored_entries)

    if entries:
        changed, recorded = apply_modes(manifest, entries, request.dry_run)
        verb = yellow('would set') if request.dry_run else green('set')
        defaults = changed - recorded
        parts = [f'{recorded} as the snapshot recorded them'] if recorded else []
        parts += [f'{defaults} at the default {DEFAULT_FILE_MODE:04o} or {DEFAULT_DIR_MODE:04o}'] if defaults > 0 else []
        detail = f' ({", ".join(parts)})' if parts else ''
        print(f'\n  {verb} modes on {bold(plural(changed, "path"))}{detail}')

    symlink_paths = ['/' + rel for rel in symlinks]
    restored_symlinks = paths_under_any([row['source'] for row in restored], symlink_paths)
    if restored_symlinks and not request.skip_symlinked:
        one = len(restored_symlinks) == 1
        was = 'was a symlink' if one else 'were symlinks'
        if request.dry_run:
            now = 'would be a real file' if one else 'would be real files'
        else:
            now = 'is now a real file' if one else 'are now real files'
        print(f'\n{yellow("note:")} {plural(len(restored_symlinks), "restored path")} {was} when backed up, and {now}:')
        for abs_path in restored_symlinks[:10]:
            print(f'  {abs_path} -> {symlinks[abs_path.lstrip("/")]}')
        if len(restored_symlinks) > 10:
            print(f'  ... and {len(restored_symlinks) - 10} more')
        print(f'  remove them and run {cyan("dotfiles link")} to restore the symlinks, or use {cyan("--skip-symlinked")} next time')

    linked_above = sorted({link for row in restored for link in symlinked_ancestors(row, symlink_paths)})
    if linked_above:
        were = 'a directory above these paths was a symlink' if len(linked_above) == 1 else 'directories above these paths were symlinks'
        wrote, became = ('would write', 'would become') if request.dry_run else ('wrote', 'is now')
        print(f'\n{yellow("note:")} {were} when backed up:')
        for abs_path in linked_above[:10]:
            print(f'  {abs_path} -> {symlinks[abs_path.lstrip("/")]}')
        if len(linked_above) > 10:
            print(f'  ... and {len(linked_above) - 10} more')
        print(f'  where that link still exists here, the restore {wrote} through it into what it points at')
        print(f'  where it does not, it {became} a real directory — use {cyan("--skip-symlinked")} to leave these paths alone')

    verb = yellow('would restore') if request.dry_run else green('restored')
    print(f'\n{bold("safekeep:")} {verb} {bold(selection_count(restored))} to {cyan(request.to)}')


def select_sources(entries, request: BackupRequest):
    """The entries a backup run covers: every one, or those matching --tag/--source.

    Bare `backup` already means everything, so these narrow rather than enable and there is no
    --all to forget. That is the opposite of restore, where selection is required and never
    inferred -- a backup that silently covered less than asked is the failure to design out,
    and a restore that silently covered more.
    """
    if not request.tag and not request.source:
        return entries
    return [
        (path, tags)
        for path, tags in entries
        if any(tag in tags for tag in request.tag) or any(needle in str(path) for needle in request.source)
    ]


def require_known_selection(config, config_path, request: BackupRequest):
    """Reject a --tag or --group that matches nothing in the config.

    A run that covers nothing reads exactly like a run that covered everything it was asked to,
    since the summary only reports what was copied. A typo has to fail rather than succeed at
    backing up nothing.
    """
    entries = config_entries(config)
    known = sorted({tag for _, _, tags in entries for tag in tags})
    unknown = [tag for tag in request.tag if tag not in known]
    if unknown:
        print(
            f'{red("safekeep:")} no source in config {cyan(config_handle(config_path))} carries {yellow(", ".join(unknown))}',
            file=sys.stderr,
        )
        print(f'  tags: {green(", ".join(known)) if known else yellow("none")}', file=sys.stderr)
        sys.exit(2)
    for needle in request.source:
        if not any(needle in str(path) for _, path, _ in entries):
            print(f'{red("safekeep:")} no path in the config contains {yellow(needle)}', file=sys.stderr)
            for _, path, _ in entries:
                print(f'    {tilde(str(path))}', file=sys.stderr)
            sys.exit(2)


def merge_manifest(existing, manifest):
    """This run's manifest folded into the one already in the snapshot.

    Reached only when two runs land in the same second and therefore share a snapshot name. That
    used to be every second run of a day, back when a snapshot was a day rather than a run.

    It is kept rather than deleted because the failure it prevents is the worst one available:
    rsync never deletes, so a second run's files land beside the first's, and writing a manifest
    that names only the second run's groups would leave the rest on disk and unrestorable. The
    manifest is the only record of what a snapshot holds.

    The scalar keys take this run's value by ordinary dict-merge, which is also what carries
    'label' correctly: do_backup writes that key only when --label was typed, so an absent flag
    leaves it out and the earlier note survives with no special case here.
    """
    if existing is None:
        return manifest

    replaced = {group_id(group) for group in manifest['groups']}
    covered = [group['source'] for group in manifest['groups']]
    merged = {**existing, **manifest}
    merged['groups'] = [g for g in existing.get('groups', []) if group_id(g) not in replaced] + manifest['groups']
    merged['modes'] = {**existing.get('modes', {}), **manifest['modes']}
    merged['symlinks'] = {**existing.get('symlinks', {}), **manifest['symlinks']}
    # This run's verdict on an oversized file replaces the old one, but only for the sources it
    # actually walked.
    merged['skipped_large'] = [
        skipped for skipped in existing.get('skipped_large', []) if not paths_under_any(covered, [skipped['path']])
    ] + manifest['skipped_large']
    return merged


def do_backup(config, config_path, warnings, request: BackupRequest):
    print(f'{bold("safekeep:")} using config {cyan(config_handle(config_path))}', flush=True)
    if request.tag or request.source:
        require_known_selection(config, config_path, request)
        print(f'  {yellow("narrowed to")} sources matching {bold(", ".join(request.tag + request.source))}', flush=True)

    start_time = time.monotonic()
    dest = Path(config['back_up_to']).expanduser()
    excludes = config.get('skip_names_matching', DEFAULT_SKIP_NAMES)
    max_size_mb = config.get('skip_files_over_mb')

    # Checked against the nearest directory that exists, so a dry run can answer without creating it.
    nearest = next(path for path in (dest, *dest.parents) if path.exists())
    if not os.access(nearest, os.W_OK):
        problem = 'is not writable' if nearest == dest else f'cannot be created, because {nearest} is not writable'
        print(f'{red("safekeep:")} destination {yellow(str(dest))} {problem}', file=sys.stderr)
        sys.exit(1)
    if request.dry_run:
        if nearest != dest:
            print(f'  {yellow("would create")} {cyan(str(dest))}', flush=True)
    else:
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f'{red("safekeep:")} cannot create destination {yellow(str(dest))} — {e}', file=sys.stderr)
            sys.exit(1)

    snapshot_name = dt.datetime.now().strftime(SNAPSHOT_FORMAT)
    dest_base = dest / snapshot_name
    link_dest = previous_snapshot(dest, snapshot_name)

    manifest = {
        'version': MANIFEST_VERSION,
        # What wrote this snapshot, beside the format version it wrote. Additive,
        # so older snapshots still read; the point is that a future format change
        # is diagnosable rather than mysterious -- 'version' says what the shape
        # is, this says which build chose that shape.
        'safekeep_version': tool_version(),
        'created': dt.datetime.now().isoformat(timespec='seconds'),
        'hostname': os.uname().nodename,
        'home': str(Path.home()),
        'config_name': config_path.stem,
        # Which snapshot this one shares inodes with. Filled in after the copying, from inodes
        # this run observed sharing -- see link_source_of. None means a full copy.
        'linked_from': None,
        'excludes': excludes,
        'max_file_size_mb': max_size_mb,
        'default_file_mode': f'{DEFAULT_FILE_MODE:04o}',
        'default_dir_mode': f'{DEFAULT_DIR_MODE:04o}',
        'config_warnings': warnings,
        'groups': [],
        'modes': {},
        'symlinks': {},
        'skipped_large': [],
    }

    # Set only when the flag was typed, because the key's *presence* is what carries that fact
    # through the merge below, which two runs inside one second still reach. `--label ''` is a
    # typed decision rather than an omission, which is why an empty string is stored as null
    # rather than skipped.
    if request.label is not None:
        manifest['label'] = request.label.strip() or None

    entries = select_sources(normalize_entries(config.get('back_up_paths', [])), request)
    if entries:
        print(f'\n{bold("paths:")}')
        present = []
        for path, tags in entries:
            if not path.exists():
                print(f'  {yellow("skip:")} {path} {yellow("(not found)")}')
                continue
            survey = survey_tree(path, excludes, max_size_mb)
            merge_survey(manifest, survey)
            manifest['groups'].append(
                {'kind': 'path', 'source': str(path), 'tags': tags, 'files': survey['files'], 'bytes': survey['bytes']}
            )
            present.append(path)
        copied = rsync_paths(present, dest_base, excludes, request.dry_run, max_size_mb, link_dest)
        print(f'  {copy_tally(request.dry_run, copied, plural(len(present), "source"))}')

    repos, back_up_untracked, ignored_patterns = repo_entries(config)
    repos = select_sources(repos, request)
    if repos and back_up_untracked:
        print(f'\n{bold("untracked:")}')
        for repo_path, tags in repos:
            if not repo_path.exists():
                print(f'  {yellow("skip:")} {yellow(str(repo_path))} (not found)')
                continue
            untracked = git_ls_untracked(repo_path)
            filtered = [f for f in untracked if not matches_exclude(str(f), excludes)]
            survey = survey_files(filtered, max_size_mb)
            merge_survey(manifest, survey)
            copyable = [f for f in filtered if snapshot_rel(f) not in {s['path'].lstrip('/') for s in survey['skipped_large']}]
            manifest['groups'].append(
                {
                    'kind': 'git_untracked',
                    'source': str(repo_path),
                    'tags': tags,
                    'files': survey['files'],
                    'bytes': survey['bytes'],
                    # The file list, so a restore can say which of a repo's files it just wrote
                    # was untracked and which was gitignored. Only the git groups carry one --
                    # see file_kinds.
                    'paths': [snapshot_rel(f) for f in copyable],
                }
            )
            copied = rsync_untracked(copyable, dest_base, request.dry_run, link_dest)
            print(f'  {copy_tally(request.dry_run, copied, cyan(tilde(str(repo_path))))}')

    if ignored_patterns and repos:
        print(f'\n{bold("ignored:")}')
        for repo_path, tags in repos:
            if not repo_path.exists():
                continue
            ignored = git_ls_ignored(repo_path, ignored_patterns)
            filtered = [f for f in ignored if not matches_exclude(str(f), excludes)]
            if not filtered:
                continue
            survey = survey_files(filtered, max_size_mb)
            merge_survey(manifest, survey)
            copyable = [f for f in filtered if snapshot_rel(f) not in {s['path'].lstrip('/') for s in survey['skipped_large']}]
            manifest['groups'].append(
                {
                    'kind': 'git_ignored',
                    'source': str(repo_path),
                    'tags': tags,
                    'files': survey['files'],
                    'bytes': survey['bytes'],
                    'paths': [snapshot_rel(f) for f in copyable],
                }
            )
            copied = rsync_untracked(copyable, dest_base, request.dry_run, link_dest)
            print(f'  {copy_tally(request.dry_run, copied, cyan(tilde(str(repo_path))))}')

    total_files = sum(g['files'] for g in manifest['groups'])
    total_bytes = sum(g['bytes'] for g in manifest['groups'])

    written = manifest
    if not request.dry_run:
        dest_base.mkdir(parents=True, exist_ok=True)
        manifest['linked_from'] = link_source_of(dest_base, link_dest)
        written = merge_manifest(read_manifest(dest_base), manifest)
        (dest_base / MANIFEST_NAME).write_text(json.dumps(written, indent=2) + '\n')

    if manifest['skipped_large']:
        print(f'\n{yellow("skipped")} {bold(plural(len(manifest["skipped_large"]), "file"))} over {max_size_mb} MB')

    if warnings:
        print()
        for warning in warnings:
            print(f'{yellow("config warning:")} {warning}')

    elapsed = time.monotonic() - start_time
    elapsed_str = f'{elapsed:.0f}s' if elapsed < 60 else f'{elapsed / 60:.1f}m'
    verb = yellow('would back up') if request.dry_run else green('backed up')
    summary = f'{bold(plural(total_files, "file"))} ({bold(human_size(total_bytes))})'
    print(f'\n{bold("safekeep:")} {verb} {summary} to {cyan(str(dest_base))} in {bold(elapsed_str)}')
    # Observed after the copy, because rsync exits 0 and names nothing when a link falls back to a copy.
    if not request.dry_run:
        print(f'  unchanged files: {link_verdict(manifest["linked_from"])}')

    # Read back off the merged manifest rather than off the request, so a run that passed no --label
    # still reports the label an earlier run in the same second left on this snapshot.
    if written.get('label'):
        print(f'  labeled {green(written["label"])}')


def init_config(name):
    """Write the annotated example as a new config, by name or at a path."""
    if names_a_path(name):
        # Absolute, as -c resolves it, so the commands printed below run from any directory.
        config_path = Path(name).expanduser().absolute()
        if not config_path.parent.is_dir():
            print(f'{red("safekeep:")} no directory {yellow(str(config_path.parent))} to write {config_path.name} into', file=sys.stderr)
            sys.exit(1)
    else:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        config_path = CONFIG_DIR / f'{name}.toml'

    if config_path.exists():
        print(f'{yellow("safekeep:")} config already exists: {cyan(str(config_path))}', file=sys.stderr)
        print(f'  change it: {cyan(f"{safekeep_for(config_path)} config edit")}', file=sys.stderr)
        sys.exit(1)

    config_path.write_text(CONFIG_TEMPLATE)
    # Read after the write, so a first config's commands carry no -c.
    command = safekeep_for(config_path)

    print(f'{green("safekeep:")} created {cyan(str(config_path))}')
    print()
    print('  set back_up_to and the sources, then see what a backup would copy:')
    print(f'    {cyan(f"{command} config edit")}')
    print(f'    {cyan(f"{command} backup run -n")}')
    print(f'  every key, explained: {cyan("safekeep config example")}')
    if config_path.parent == CONFIG_DIR and len(config_names()) > 1:
        print(f'  with more than one config, every command names one: {cyan(f"{command} …")}')


def edit_config(config_path):
    """Open a config in $VISUAL or $EDITOR, then read it back.

    Reading it back is the point. load_config is fatal on a rename or a parse error, so the edit
    that introduced one is reported while the file is still in hand rather than at the start of
    the next backup. A terminal editor blocks until it closes; a GUI editor returns immediately
    and the check then describes the file as it was, unless it is configured to wait
    (EDITOR="code --wait").
    """
    editor = os.environ.get('VISUAL') or os.environ.get('EDITOR')
    if not editor:
        print(f'{red("safekeep:")} no editor set — set {yellow("$VISUAL")} or {yellow("$EDITOR")}', file=sys.stderr)
        print(f'  the file is at {cyan(str(config_path))}', file=sys.stderr)
        sys.exit(1)

    # An editor may carry arguments ("code --wait", "emacsclient -nw"), so it is split rather
    # than run as one word, and the binary resolved to a full path.
    parts = shlex.split(editor)
    binary = shutil.which(parts[0])
    if binary is None:
        print(f'{red("safekeep:")} editor not found: {yellow(parts[0])}', file=sys.stderr)
        sys.exit(1)

    subprocess.run([binary, *parts[1:], str(config_path)], check=False)

    config, warnings = load_config(config_path)
    entries = normalize_entries(config.get('back_up_paths', []))
    repos, _, _ = repo_entries(config)
    summary = f'{plural(len(entries), "path")}, {plural(len(repos), "git repo")}'
    print(f'{green("safekeep:")} {cyan(str(config_path))} — {summary}')
    for warning in warnings:
        print(f'  {yellow("config warning:")} {warning}')


def show_config(config_path, config, warnings, as_json=False):
    """Display the resolved config with readable formatting."""
    if as_json:
        repos, back_up_untracked, ignored_patterns = repo_entries(config)
        print_json(
            {
                'path': str(config_path),
                'back_up_to': config['back_up_to'],
                'back_up_paths': [{'path': str(path), 'tags': tags} for path, tags in normalize_entries(config.get('back_up_paths', []))],
                'git': {
                    'repos': [{'path': str(path), 'tags': tags} for path, tags in repos],
                    'back_up_untracked_files': back_up_untracked,
                    'back_up_ignored_files_matching': ignored_patterns,
                },
                'skip_names_matching': config.get('skip_names_matching', DEFAULT_SKIP_NAMES),
                'skip_files_over_mb': config.get('skip_files_over_mb'),
                'warnings': warnings,
            }
        )
        return

    print(f'{bold("safekeep:")} {cyan(str(config_path))}')
    print()
    print(f'  {bold("back up to:")} {cyan(config["back_up_to"])}')

    entries = normalize_entries(config.get('back_up_paths', []))
    repos, back_up_untracked, ignored_patterns = repo_entries(config)

    if entries:
        print(f'\n  {bold("back up")} {plural(len(entries), "path")}:')
        for path, tags in entries:
            suffix = f'  [{", ".join(tags)}]' if tags else ''
            print(f'    {path}{suffix}')

    if repos:
        print(f'\n  {bold("in")} {plural(len(repos), "git repo")}:')
        for path, tags in repos:
            suffix = f'  [{", ".join(tags)}]' if tags else ''
            print(f'    {path}{suffix}')
        untracked_label = green('back up') if back_up_untracked else yellow('do not back up')
        print(f'    {untracked_label} untracked files')
        if ignored_patterns:
            print(f'    {green("back up")} ignored files matching {", ".join(ignored_patterns)}')

    if not entries and not repos:
        print(f'\n  {yellow("nothing to back up: no back_up_paths, no git.repos")}')

    excludes = config.get('skip_names_matching', DEFAULT_SKIP_NAMES)
    print(f'\n  {bold("skip names matching:")} {", ".join(excludes)}')
    max_size_mb = config.get('skip_files_over_mb')
    if max_size_mb is not None:
        print(f'  {bold("skip files over:")} {max_size_mb} MB')

    if warnings:
        print()
        for warning in warnings:
            print(f'  {yellow("config warning:")} {warning}')
