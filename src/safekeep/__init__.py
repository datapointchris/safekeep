"""safekeep - Config-driven file preservation with self-describing dated snapshots.

Rsync-copies files and directories to a destination as dated snapshots, and writes a
manifest into each one recording what was collected, the source file modes, and which
sources were symlinks. That manifest is what makes a snapshot restorable without the
config that produced it -- the disaster-recovery case, where the config died with the
machine.

Primary use case: backing up scattered config files, local scripts, and git-untracked
WIP to a network drive that cannot represent Unix modes.

Config: ~/.config/safekeep/<name>.toml (the manifest stays JSON -- machines write it)

Bare `safekeep` prints usage. Nothing writes without an explicit verb. The command
surface is not repeated here -- `safekeep --help` is the one copy of it.
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import datetime
from fnmatch import fnmatch
from functools import cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as installed_version
from pathlib import Path

from pyselfupdate import Config
from pyselfupdate import SelfUpdateError
from pyselfupdate import notify
from pyselfupdate import update
from pytermstyle import bold
from pytermstyle import clip
from pytermstyle import cyan
from pytermstyle import green
from pytermstyle import help_end
from pytermstyle import help_header
from pytermstyle import help_row
from pytermstyle import help_section
from pytermstyle import help_text
from pytermstyle import help_usage
from pytermstyle import red
from pytermstyle import yellow

# Keys are phrases that state what safekeep will do, so the file reads as a description of the
# backup rather than a dump of this program's variables -- see ~/dev/standards/configuration.md.
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

CONFIG_DIR = Path.home() / '.config' / 'safekeep'

SINGLE_TAG_EXAMPLE = 'tags = ["wsl"]'

MANIFEST_NAME = '.safekeep-manifest.json'
MANIFEST_VERSION = 1


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


# Notify-only, per ~/dev/standards/release.md. One check per 24h, one line to
# stderr, and `safekeep update` is the only thing that writes anything.
UPDATE_CONFIG = Config(tool='safekeep', owner='datapointchris')

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
# labels: `safekeep restore --tag secrets` restores just those groups, so tag by
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


def plural(count, noun):
    return f'{count} {noun}' if count == 1 else f'{count} {noun}s'


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


def warn_about_json_configs():
    """Name the leftover JSON configs, since 'no configs found' is a bewildering way to
    report a format change to someone whose config file is sitting right there."""
    leftovers = sorted(CONFIG_DIR.glob('*.json')) if CONFIG_DIR.exists() else []
    if not leftovers:
        return
    print(f'  {yellow("configs are TOML now")}, and these are still JSON:', file=sys.stderr)
    for path in leftovers:
        print(f'    {path.name} -> {path.stem}.toml', file=sys.stderr)


def resolve_config(name):
    """Resolve config by name, absolute path, or auto-detect single config."""
    if name:
        path = Path(name)
        if path.is_absolute() and path.exists():
            return path
        config_path = CONFIG_DIR / f'{name}.toml'
        if config_path.exists():
            return config_path
        print(f'{red("safekeep:")} config not found: {yellow(name)}', file=sys.stderr)
        print(f'  looked in: {cyan(str(config_path))}', file=sys.stderr)
        warn_about_json_configs()
        print(f'  generate it: {cyan(f"safekeep config init {name}")}', file=sys.stderr)
        sys.exit(1)

    if not CONFIG_DIR.exists():
        print(f'{red("safekeep:")} no config directory at {cyan(str(CONFIG_DIR))}', file=sys.stderr)
        print(f'  generate a starter config: {cyan("safekeep config init")}', file=sys.stderr)
        sys.exit(1)

    configs = sorted(CONFIG_DIR.glob('*.toml'))
    if not configs:
        print(f'{red("safekeep:")} no configs found in {cyan(str(CONFIG_DIR))}', file=sys.stderr)
        warn_about_json_configs()
        print(f'  generate a starter config: {cyan("safekeep config init")}', file=sys.stderr)
        sys.exit(1)
    if len(configs) == 1:
        return configs[0]

    print(f'{yellow("safekeep:")} multiple configs found, specify one with {cyan("--config")}:', file=sys.stderr)
    for c in configs:
        print(f'  {green(c.stem)}', file=sys.stderr)
    sys.exit(1)


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
            print(f'{red("safekeep:")} every entry needs a "path" key: {yellow(repr(entry))}', file=sys.stderr)
            sys.exit(1)
        tags = entry.get('tags', [])
        # Fatal rather than coerced: a bare tags = "wsl" is a list of characters to Python, so
        # the entry ends up tagged w, s and l, and the only symptom is `restore --tag wsl`
        # selecting nothing from a snapshot whose config plainly carries the tag.
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            print(f'{red("safekeep:")} "tags" must be a list of strings: {yellow(repr(entry))}', file=sys.stderr)
            print(f'  a single tag is still a list: {cyan(SINGLE_TAG_EXAMPLE)}', file=sys.stderr)
            sys.exit(1)
        normalized.append((Path(entry['path']).expanduser(), list(tags)))
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
        if dir_mode != DEFAULT_DIR_MODE and current != root:
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


def rsync_paths(paths, dest_base, excludes, dry_run=False, max_size_mb=None):
    """Rsync absolute paths into dest_base, preserving full directory structure.

    Uses rsync --relative with absolute paths so that '/home/chris/.ssh/config'
    becomes dest_base/home/chris/.ssh/config.
    """
    valid = [str(p) for p in paths if p.exists()]
    if not valid:
        return

    if not dry_run:
        dest_base.mkdir(parents=True, exist_ok=True)

    cmd = ['rsync', '-av', '--no-perms', '--chmod=Du+w', '--relative', '--copy-links']
    for pattern in excludes:
        cmd.extend(['--exclude', pattern])
    if max_size_mb is not None:
        cmd.extend(['--max-size', f'{max_size_mb}m'])
    if dry_run:
        cmd.append('-n')
    cmd.extend(valid)
    cmd.append(str(dest_base) + '/')

    run_rsync(cmd)


def rsync_untracked(files, dest_base, dry_run=False):
    """Rsync individual untracked files preserving full path structure.

    Uses --files-from with / as the base for efficiency when copying many
    small files. Paths are stored relative to filesystem root in the destination.
    """
    rel_paths = [snapshot_rel(f) for f in files]
    if not rel_paths:
        return

    if not dry_run:
        dest_base.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as tmp:
        tmp.write('\n'.join(rel_paths) + '\n')
        tmp_path = tmp.name

    try:
        cmd = ['rsync', '-av', '--no-perms', '--files-from', tmp_path, '/', str(dest_base) + '/']
        if dry_run:
            cmd.append('-n')
        run_rsync(cmd)
    finally:
        os.unlink(tmp_path)


@cache
def rsync_progress_flag():
    """The flag that makes rsync report as it works, or None where it would only make a mess.

    --info=progress2 is one self-overwriting line covering the whole transfer; --progress is a
    block per file, which on a real restore is thousands of lines of scroll. rsync grew --info
    in 3.1, and a restore does not get to assume the good rsync: macOS ships openrsync as
    /usr/bin/rsync, and a disaster recovery is precisely the moment the Homebrew rsync this repo
    declares has not been installed yet. Both flags redraw their own line, so a non-terminal gets
    neither and reads the per-group lines instead.
    """
    if not sys.stdout.isatty():
        return None
    probe = subprocess.run(['rsync', '--info=help'], capture_output=True)
    return '--info=progress2' if probe.returncode == 0 else '--progress'


def run_rsync(cmd):
    """Run rsync, tolerating the partial-transfer exit codes.

    Any other failure exits rather than raising: a traceback names the Python frame that called
    rsync, which is never the thing that went wrong, and it buries the command. Re-running that
    command by hand is how an rsync failure gets diagnosed, so it is what the error prints.
    """
    result = subprocess.run(cmd)
    if result.returncode in (23, 24):
        print(f'  {yellow("warning:")} rsync completed with partial transfer (some files skipped)')
        return
    if result.returncode != 0:
        print(f'{red("safekeep:")} rsync exited {yellow(str(result.returncode))}', file=sys.stderr)
        print(f'  {shlex.join(cmd)}', file=sys.stderr)
        sys.exit(1)


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
    """List dated snapshot directories at dest, newest first, paired with manifests."""
    if not dest.exists():
        return []
    dated = [d for d in dest.iterdir() if d.is_dir() and len(d.name) == 10 and d.name[4] == '-' and d.name[7] == '-']
    return [(d, read_manifest(d)) for d in sorted(dated, key=lambda d: d.name, reverse=True)]


def group_id(group):
    return f'{group["kind"]}:{group["source"]}'


def show_snapshots(dest):
    snapshots = list_snapshots(dest)
    if not snapshots:
        print(f'{yellow("safekeep:")} no snapshots at {cyan(str(dest))}')
        return

    print(f'{bold("safekeep:")} {len(snapshots)} snapshots at {cyan(str(dest))}')
    print()
    for snapshot_dir, manifest in snapshots:
        if manifest is None:
            print(f'  {bold(snapshot_dir.name)}  {yellow("no manifest — not restorable by safekeep")}')
            continue
        groups = manifest.get('groups', [])
        total_bytes = sum(g.get('bytes', 0) for g in groups)
        total_files = sum(g.get('files', 0) for g in groups)
        host = manifest.get('hostname', '?')
        print(f'  {bold(snapshot_dir.name)}  {human_size(total_bytes):>9}  {total_files:>6} files  {len(groups):>2} groups  {cyan(host)}')


def preview_snapshot(dest, date):
    """Render a snapshot summary for the fzf preview pane."""
    manifest = read_manifest(dest / date)
    if manifest is None:
        print('no manifest — not restorable by safekeep')
        return
    print(f'{date}   {manifest.get("hostname", "?")}   {manifest.get("created", "?")}')
    print(f'config: {manifest.get("config_name", "?")}   home: {manifest.get("home", "?")}')
    print()
    for group in manifest.get('groups', []):
        tags = ' '.join(group.get('tags', []))
        print(f'  {group["kind"]:<14} {group["source"]}')
        print(f'  {"":<14} {plural(group.get("files", 0), "file")}, {human_size(group.get("bytes", 0))}  {tags}')
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


def snapshot_to_size_against(dest, date):
    """The snapshot a tag listing reports against: the one named, or the newest restorable one."""
    snapshots = [(d, m) for d, m in list_snapshots(dest) if m is not None]
    if date is None:
        return snapshots[0] if snapshots else (None, None)
    for snapshot_dir, manifest in snapshots:
        if snapshot_dir.name == date:
            return snapshot_dir, manifest
    print(f'{red("safekeep:")} no restorable snapshot {yellow(date)} at {cyan(str(dest))}', file=sys.stderr)
    sys.exit(1)


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


def tag_index(config, sources):
    """{tag: [row]} over the config and a snapshot together.

    Either side alone hides one of the two ways a tagged restore comes back empty: a tag added
    since the snapshot was taken selects nothing in it, and a tag renamed in the config is still
    the only name the snapshots taken before the rename answer to. A row's 'files' is None when
    the source is not in the snapshot at all, which is the first case.
    """
    index = {}
    entries = {str(path): (kind, tags) for kind, path, tags in config_entries(config)}

    for source in sorted(set(entries) | set(sources)):
        kind, config_tags = entries.get(source, (None, []))
        stored = sources.get(source)
        for tag in dict.fromkeys(config_tags + (stored['tags'] if stored else [])):
            if source not in entries:
                note = 'not in the config'
            elif tag not in config_tags:
                note = 'tagged in the snapshot only'
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


def sized_total(rows):
    """The files and bytes rows account for in the snapshot, ignoring those not in it."""
    sized = [row for row in rows if row['files'] is not None]
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
    if snapshot_dir is None:
        print(f'  in {cyan(config_path.name)} — {yellow("no snapshots")} at {cyan(str(dest))} to restore by tag yet')
    else:
        print(f'  in {cyan(config_path.name)}, sized against snapshot {cyan(snapshot_dir.name)}')


def show_tags(config, config_path, args):
    """List the tags a restore can select on, and what each would bring back."""
    dest = Path(config['back_up_to']).expanduser()
    snapshot_dir, manifest = snapshot_to_size_against(dest, args.from_date)
    index = tag_index(config, snapshot_sources(manifest))

    if args.name:
        show_tag(args.name, index, config_path, dest, snapshot_dir, args.from_date)
        return

    print(f'{bold("safekeep:")} {plural(len(index), "tag")}')
    print_tag_sources(config_path, dest, snapshot_dir)

    if not index:
        print(f'\n  Tag the entries and a restore can select them: {cyan("safekeep config edit")}')
        return

    print()
    width = max(len(tag) for tag in index)
    for tag in sorted(index):
        rows = index[tag]
        # A tag half of whose sources are missing still shows a size, and the size is the part
        # that reads as reassuring -- so the shortfall is named on the same row rather than left
        # to be noticed by drilling in. With no snapshot at all the header has said so already.
        missing = [row for row in rows if row['files'] is None]
        if snapshot_dir is None:
            sizes = ''
        elif len(missing) == len(rows):
            sizes = yellow('not in this snapshot')
        else:
            shortfall = f'  {yellow(f"{len(missing)} not in this snapshot")}' if missing else ''
            sizes = f'{sized_total(rows)}{shortfall}'
        print(f'  {green(f"{tag:<{width}}")}  {plural(len(rows), "source"):<12}{sizes}'.rstrip())

    untagged = [path for _, path, tags in config_entries(config) if not tags]
    if untagged:
        print(f'\n  untagged: {plural(len(untagged), "source")} — only {cyan("--all")} or {cyan("--group")} reaches them')
    print(f'\n  {cyan(f"safekeep tags {sorted(index)[0]}")}  what one tag covers')


def show_tag(name, index, config_path, dest, snapshot_dir, date):
    """Show the sources one tag covers, and the restore that would bring them back."""
    rows = index.get(name)
    if not rows:
        print(f'{red("safekeep:")} no tag {yellow(name)} in {cyan(config_path.name)}', file=sys.stderr)
        if index:
            print(f'  tags: {green(", ".join(sorted(index)))}', file=sys.stderr)
        sys.exit(2)

    print(f'{bold("safekeep:")} tag {green(name)} covers {bold(plural(len(rows), "source"))}')
    print_tag_sources(config_path, dest, snapshot_dir)
    print()

    width = max(len(tilde(row['source'])) for row in rows)
    for row in rows:
        if snapshot_dir is None:
            sizes = ''
        else:
            sizes = size_cell(row['files'], row['bytes']) if row['files'] is not None else yellow('not in this snapshot')
        note = f'  {yellow(row["note"])}' if row['note'] else ''
        print(f'  {row["kind"]:<9} {tilde(row["source"]):<{width}}  {sizes}{note}'.rstrip())

    # A total under a single row is the same number twice.
    if len([row for row in rows if row['files'] is not None]) > 1:
        print(f'  {"":<9} {"":<{width}}  {bold(sized_total(rows))}')

    from_flag = f' --from {date}' if date else ''
    print(f'\n  restore it: {cyan(f"safekeep restore --to /tmp/rehearsal{from_flag} --tag {name}")}')


def require_fzf():
    if shutil.which('fzf'):
        return
    print(f'{red("safekeep:")} fzf is required for interactive selection', file=sys.stderr)
    print(f'  select non-interactively instead: {cyan("--all")}, {cyan("--group PATH")}, or {cyan("--tag NAME")}', file=sys.stderr)
    sys.exit(1)


def fzf(lines, args):
    """Run fzf over lines, returning the selected ones."""
    result = subprocess.run(['fzf', *args], input='\n'.join(lines), capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def pick_snapshot(dest, config_name):
    """Interactively choose a snapshot, previewing each one's manifest."""
    snapshots = [(d, m) for d, m in list_snapshots(dest) if m is not None]
    if not snapshots:
        print(f'{red("safekeep:")} no restorable snapshots at {cyan(str(dest))}', file=sys.stderr)
        sys.exit(1)

    # `-m safekeep` rather than this file's path: as a package, __file__ is
    # src/safekeep/__init__.py, and running that directly re-imports the module
    # under the name __main__ instead of resolving the installed package.
    preview_cmd = f'{sys.executable} -m safekeep --config {config_name} preview-snapshot {{1}}'

    lines = []
    for snapshot_dir, manifest in snapshots:
        groups = manifest.get('groups', [])
        total_bytes = sum(g.get('bytes', 0) for g in groups)
        lines.append(f'{snapshot_dir.name}\t{human_size(total_bytes)}\t{plural(len(groups), "group")}')

    selected = fzf(
        lines,
        [
            '--delimiter=\t',
            '--with-nth=1,2,3',
            '--header=select a snapshot   ↑↓ move · enter choose · esc cancel',
            '--header-first',
            '--preview',
            preview_cmd,
            '--preview-window=right:60%',
        ],
    )
    if not selected:
        return None
    return selected[0].split('\t')[0]


def pick_groups(snapshot_dir, manifest):
    """Interactively choose groups, previewing each one's subtree in the snapshot."""
    groups = manifest.get('groups', [])
    if not groups:
        return []

    lines = []
    for group in groups:
        tags = ','.join(group.get('tags', [])) or '-'
        lines.append(
            f'{group["kind"]}\t{group["source"]}\t{plural(group.get("files", 0), "file")}\t{human_size(group.get("bytes", 0))}\t{tags}'
        )

    selected = fzf(
        lines,
        [
            '--multi',
            '--delimiter=\t',
            '--with-nth=1,2,3,4,5',
            # Named in full and pinned above the prompt: the multi-select keys are fzf's own, so
            # the one place they can be recalled is the picker itself.
            '--header=tab select · shift-tab deselect · enter restore · esc cancel',
            '--header-first',
            '--preview',
            f'ls -la {snapshot_dir}{{2}} 2>/dev/null | head -60',
            '--preview-window=right:60%',
        ],
    )

    chosen = {(line.split('\t')[0], line.split('\t')[1]) for line in selected}
    return [g for g in groups if (g['kind'], g['source']) in chosen]


def select_groups(manifest, args):
    """Resolve which groups to restore from flags, or None if selection is interactive."""
    groups = manifest.get('groups', [])
    if args.all:
        return groups

    if not args.group and not args.tag:
        return None

    selected = []
    for group in groups:
        matched_group = any(needle in group['source'] for needle in args.group)
        matched_tag = any(tag in group.get('tags', []) for tag in args.tag)
        if matched_group or matched_tag:
            selected.append(group)
    return selected


def remap_home(source, manifest_home, target_home):
    """Rewrite a source path recorded under the backup machine's home into this one's."""
    if manifest_home in (None, target_home):
        return source
    if source == manifest_home:
        return target_home
    if source.startswith(manifest_home + '/'):
        return target_home + source[len(manifest_home) :]
    return source


def paths_under(source, candidates):
    """Absolute paths from candidates that are the source itself or live beneath it."""
    prefix = str(source).rstrip('/') + '/'
    return [p for p in candidates if p == str(source) or p.startswith(prefix)]


def paths_under_any(sources, candidates):
    """Absolute paths from candidates covered by any of the sources, deduplicated."""
    return sorted({p for source in sources for p in paths_under(source, candidates)})


def restore_group(snapshot_dir, source, target_root, manifest_home, target_home, on_conflict, skip_symlinked, symlinks, dry_run):
    """Rsync one group's subtree out of the snapshot and into target_root."""
    stored = snapshot_dir / snapshot_rel(source)
    if not stored.exists():
        print(f'    {yellow("skip: not present in snapshot")}')
        return False

    symlinked = paths_under(source, ['/' + rel for rel in symlinks])
    if skip_symlinked and str(source) in symlinked:
        print(f'    {yellow("skip: was a symlink")}')
        return False

    target = Path(target_root) / snapshot_rel(remap_home(source, manifest_home, target_home))

    cmd = ['rsync', '-a']
    if on_conflict == 'skip':
        cmd.append('--ignore-existing')
    elif on_conflict == 'newer':
        cmd.append('--update')
    else:
        # backup and overwrite both mean the snapshot wins, so the quick check has to go.
        # rsync skips a file with the same size and mtime without ever reading it, which is
        # right for a sync and wrong for a restore: a file corrupted in place keeps both its
        # size and its timestamp, and that is precisely the file being restored over. Checksum
        # rather than --ignore-times, so genuinely identical files are still skipped and no
        # .pre-restore copy is manufactured for a file that never changed.
        cmd.append('--checksum')
        if on_conflict == 'backup':
            cmd.extend(['--backup', '--suffix=.pre-restore'])

    if skip_symlinked and stored.is_dir():
        for abs_path in symlinked:
            cmd.extend(['--exclude', '/' + os.path.relpath(abs_path, str(source))])

    if dry_run:
        cmd.append('-n')

    progress = rsync_progress_flag()
    if progress and not dry_run:
        cmd.append(progress)

    if stored.is_dir():
        cmd.append(str(stored) + '/')
        cmd.append(str(target) + '/')
    else:
        cmd.append(str(stored))
        cmd.append(str(target))

    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
    elif not target.parent.exists():
        # A rehearsal into a fresh directory, which is the documented way to use --dry-run. The
        # parent cannot be created here without writing, and rsync will not accept a file
        # destination whose directory is missing -- it exits 3 rather than reporting a transfer.
        # Nothing exists to conflict with, so every file under the group would be created and
        # there is no question left for rsync to answer.
        return True

    run_rsync(cmd)
    return True


def apply_modes(manifest, sources, target_root, dry_run):
    """Reapply source file modes lost to the destination filesystem.

    Walks each restored subtree applying the recorded defaults, then overrides with the
    manifest's deviations. Without this every restored file carries whatever mode the
    SMB round-trip gave it, and ssh and gpg refuse to use their own config files.
    """
    manifest_home = manifest.get('home')
    target_home = str(Path.home())
    modes = manifest.get('modes', {})
    changed = 0

    if dry_run:
        # Nothing has been written, so there is no tree to walk. Report the recorded
        # deviations that would be reapplied rather than a count of zero.
        return len(paths_under_any(sources, ['/' + rel for rel in modes]))

    for source in sources:
        target = Path(target_root) / snapshot_rel(remap_home(source, manifest_home, target_home))
        if not target.exists():
            continue

        status(f'scanning {tilde(source)}')
        # Modes are keyed by the path as it existed on the backup machine, so each
        # restored path is walked back through the target root and the home remap.
        paths = [target] if target.is_file() else [target, *target.rglob('*')]
        for path in paths:
            origin = Path(source) if path == target else Path(source) / path.relative_to(target)
            recorded = modes.get(snapshot_rel(origin))
            wanted = int(recorded, 8) if recorded else (DEFAULT_DIR_MODE if path.is_dir() else DEFAULT_FILE_MODE)
            try:
                path.chmod(wanted)
            except OSError:
                continue
            changed += 1
            # One syscall per path, and over SMB that is minutes of silence on a large group.
            # The interval is a redraw budget rather than a reporting granularity.
            if changed % 200 == 0:
                status(f'reapplying modes … {plural(changed, "path")}')

    clear_status()
    return changed


def explain_empty_selection(manifest, date, args):
    """Say why an explicit selection matched nothing in this snapshot.

    Tags live in the manifest, not in the config -- each one is a copy of what the config said
    on the day the snapshot was taken. Tagging an entry today does not retag the snapshots that
    already exist, and that is the whole of why a restore comes back empty while the config
    plainly carries the tag. Nothing in "nothing selected" said so.
    """
    groups = manifest.get('groups', [])
    if args.tag:
        available = sorted({tag for group in groups for tag in group.get('tags', [])})
        print(f'  no group in {cyan(date)} carries {yellow(", ".join(args.tag))}', file=sys.stderr)
        print(f'  tags in this snapshot: {green(", ".join(available)) if available else yellow("none")}', file=sys.stderr)
        print(f'  a snapshot carries the tags its config had that day — {cyan("safekeep tags")} compares the two', file=sys.stderr)
    if args.group:
        print(f'  no source in {cyan(date)} contains {yellow(", ".join(args.group))}:', file=sys.stderr)
        for group in groups:
            print(f'    {tilde(group["source"])}', file=sys.stderr)
    if args.all and not groups:
        print(f'  {cyan(date)} records no groups at all', file=sys.stderr)


def source_totals(groups):
    """Each source once, in selection order, with the files and bytes its groups sum to.

    git_untracked and git_ignored groups share a repo subtree, so restoring per group would
    rsync the same directory twice; they are also disjoint file sets, so summing them gives
    the size the one rsync will actually move.
    """
    totals = {}
    for group in groups:
        entry = totals.setdefault(group['source'], {'files': 0, 'bytes': 0})
        entry['files'] += group.get('files', 0)
        entry['bytes'] += group.get('bytes', 0)
    return totals


def do_restore(config, config_path, args):
    dest = Path(config['back_up_to']).expanduser()

    if args.from_date:
        date = args.from_date
    elif sys.stdin.isatty() and not (args.all or args.group or args.tag):
        require_fzf()
        date = pick_snapshot(dest, config_path.stem)
        if date is None:
            print(f'{yellow("safekeep:")} nothing selected, nothing restored')
            return
    else:
        restorable = [d for d, m in list_snapshots(dest) if m is not None]
        if not restorable:
            print(f'{red("safekeep:")} no restorable snapshots at {cyan(str(dest))}', file=sys.stderr)
            sys.exit(1)
        date = restorable[0].name

    snapshot_dir = dest / date
    if not snapshot_dir.exists():
        print(f'{red("safekeep:")} no snapshot {yellow(date)} at {cyan(str(dest))}', file=sys.stderr)
        sys.exit(1)

    manifest = read_manifest(snapshot_dir)
    if manifest is None:
        print(f'{red("safekeep:")} snapshot {yellow(date)} has no manifest — safekeep cannot restore it', file=sys.stderr)
        print(f'  copy it out with rsync directly: {cyan(f"rsync -av {snapshot_dir}/ /")}', file=sys.stderr)
        sys.exit(1)

    groups = select_groups(manifest, args)
    if groups is None:
        if not sys.stdin.isatty():
            print(f'{red("safekeep:")} no groups selected — pass {cyan("--all")}, {cyan("--group")}, or {cyan("--tag")}', file=sys.stderr)
            for group in manifest.get('groups', []):
                print(f'  {group["kind"]:<14} {group["source"]}', file=sys.stderr)
            sys.exit(1)
        require_fzf()
        groups = pick_groups(snapshot_dir, manifest)

    if not groups:
        # An explicit selection that matched nothing is a failed request, not a cancelled one:
        # exit non-zero so a caller cannot read it as a restore that happened to be empty.
        if args.all or args.group or args.tag:
            print(f'{red("safekeep:")} nothing selected, nothing restored', file=sys.stderr)
            explain_empty_selection(manifest, date, args)
            sys.exit(1)
        print(f'{yellow("safekeep:")} nothing selected, nothing restored')
        return

    totals = source_totals(groups)
    manifest_home = manifest.get('home')
    target_home = str(Path.home())
    symlinks = manifest.get('symlinks', {})

    label = yellow('would restore') if args.dry_run else green('restoring')
    print(f'{bold("safekeep:")} {label} {bold(plural(len(groups), "group"))} from {cyan(date)} to {cyan(args.to)}')
    if manifest_home and manifest_home != target_home:
        print(f'  remapping {cyan(manifest_home)} -> {cyan(target_home)}')
    if args.on_conflict in ('backup', 'overwrite') and not args.dry_run:
        # Named because it is the whole reason a restore takes as long as it does, and an
        # unexplained wait reads as a hang. The other two modes skip on mtime and are quick.
        print(f'  comparing by {cyan("checksum")}, which reads every file on both sides')
    print()

    restored = []
    width = max(len(tilde(source)) for source in totals)
    for position, source in enumerate(totals, start=1):
        # Printed before the work rather than after it: this line is what says which group
        # the wait belongs to, and after the fact it says nothing that was not already known.
        counter = cyan(f'[{position}/{len(totals)}]')
        print(f'  {counter} {tilde(source):<{width}}  {size_cell(totals[source]["files"], totals[source]["bytes"])}', flush=True)
        if restore_group(
            snapshot_dir, source, args.to, manifest_home, target_home, args.on_conflict, args.skip_symlinked, symlinks, args.dry_run
        ):
            restored.append(source)

    if restored:
        count = apply_modes(manifest, restored, args.to, args.dry_run)
        if args.dry_run:
            print(f'\n  {yellow("would reapply")} {bold(plural(count, "recorded mode"))}')
        else:
            print(f'\n  {green("reapplied")} modes to {bold(plural(count, "path"))}')

    restored_symlinks = paths_under_any(restored, ['/' + rel for rel in symlinks])
    if restored_symlinks and not args.skip_symlinked:
        print(f'\n{yellow("note:")} {len(restored_symlinks)} restored paths were symlinks when backed up, and are now real files:')
        for abs_path in restored_symlinks[:10]:
            print(f'  {abs_path} -> {symlinks[abs_path.lstrip("/")]}')
        if len(restored_symlinks) > 10:
            print(f'  ... and {len(restored_symlinks) - 10} more')
        print(f'  remove them and run {cyan("dotfiles link")} to restore the symlinks, or use {cyan("--skip-symlinked")} next time')

    label = yellow('would restore') if args.dry_run else green('restored')
    print(f'\n{bold("safekeep:")} {label} {bold(plural(len(restored), "group"))} to {cyan(args.to)}')


def select_sources(entries, args):
    """The entries a backup run covers: every one, or those matching --tag/--group.

    Bare `backup` already means everything, so these narrow rather than enable and there is no
    --all to forget. That is the opposite of restore, where selection is required and never
    inferred -- a backup that silently covered less than asked is the failure to design out,
    and a restore that silently covered more.
    """
    if not args.tag and not args.group:
        return entries
    return [
        (path, tags) for path, tags in entries if any(tag in tags for tag in args.tag) or any(needle in str(path) for needle in args.group)
    ]


def require_known_selection(config, config_path, args):
    """Reject a --tag or --group that matches nothing in the config.

    A run that covers nothing reads exactly like a run that covered everything it was asked to,
    since the summary only reports what was copied. A typo has to fail rather than succeed at
    backing up nothing.
    """
    entries = config_entries(config)
    known = sorted({tag for _, _, tags in entries for tag in tags})
    unknown = [tag for tag in args.tag if tag not in known]
    if unknown:
        print(f'{red("safekeep:")} no entry in {cyan(config_path.name)} carries {yellow(", ".join(unknown))}', file=sys.stderr)
        print(f'  tags: {green(", ".join(known)) if known else yellow("none")}', file=sys.stderr)
        sys.exit(2)
    for needle in args.group:
        if not any(needle in str(path) for _, path, _ in entries):
            print(f'{red("safekeep:")} no path in the config contains {yellow(needle)}', file=sys.stderr)
            for _, path, _ in entries:
                print(f'    {tilde(str(path))}', file=sys.stderr)
            sys.exit(2)


def merge_manifest(existing, manifest):
    """This run's manifest folded into the one already in the snapshot.

    A run narrowed by --tag or --group still rewrites the manifest of a snapshot that may
    already hold a full backup. rsync never deletes, so the files it did not touch are still
    there -- dropping their groups would leave them on disk and unrestorable, which is the
    exact failure the manifest exists to prevent.
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


def do_backup(config, config_path, warnings, args):
    print(f'{bold("safekeep:")} using config {cyan(config_path.name)}', flush=True)
    if args.tag or args.group:
        require_known_selection(config, config_path, args)
        print(f'  {yellow("narrowed to")} sources matching {bold(", ".join(args.tag + args.group))}', flush=True)

    start_time = time.monotonic()
    dest = Path(config['back_up_to']).expanduser()
    excludes = config.get('skip_names_matching', DEFAULT_SKIP_NAMES)
    max_size_mb = config.get('skip_files_over_mb')

    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f'{red("safekeep:")} cannot create destination {yellow(str(dest))} — {e}', file=sys.stderr)
        sys.exit(1)
    if not os.access(dest, os.W_OK):
        print(f'{red("safekeep:")} destination {yellow(str(dest))} is not writable', file=sys.stderr)
        sys.exit(1)

    date_dir = datetime.now().strftime('%Y-%m-%d')
    dest_base = dest / date_dir

    manifest = {
        'version': MANIFEST_VERSION,
        # What wrote this snapshot, beside the format version it wrote. Additive,
        # so older snapshots still read; the point is that a future format change
        # is diagnosable rather than mysterious -- 'version' says what the shape
        # is, this says which build chose that shape.
        'safekeep_version': tool_version(),
        'created': datetime.now().isoformat(timespec='seconds'),
        'hostname': os.uname().nodename,
        'home': str(Path.home()),
        'config_name': config_path.stem,
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

    entries = select_sources(normalize_entries(config.get('back_up_paths', [])), args)
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
        rsync_paths(present, dest_base, excludes, args.dry_run, max_size_mb)
        label = yellow('would copy') if args.dry_run else green('copied')
        print(f'  {label} {bold(plural(len(present), "path"))}')

    repos, back_up_untracked, ignored_patterns = repo_entries(config)
    repos = select_sources(repos, args)
    if repos and back_up_untracked:
        print(f'\n{bold("git_untracked:")}')
        for repo_path, tags in repos:
            if not repo_path.exists():
                print(f'  {yellow("skip:")} {yellow(str(repo_path))} (not found)')
                continue
            untracked = git_ls_untracked(repo_path)
            filtered = [f for f in untracked if not matches_exclude(str(f), excludes)]
            survey = survey_files(filtered, max_size_mb)
            merge_survey(manifest, survey)
            manifest['groups'].append(
                {'kind': 'git_untracked', 'source': str(repo_path), 'tags': tags, 'files': survey['files'], 'bytes': survey['bytes']}
            )
            copyable = [f for f in filtered if snapshot_rel(f) not in {s['path'].lstrip('/') for s in survey['skipped_large']}]
            rsync_untracked(copyable, dest_base, args.dry_run)
            label = yellow('would copy') if args.dry_run else green('copied')
            print(f'  {label} {bold(plural(survey["files"], "untracked file"))} from {cyan(str(repo_path))}')

    if ignored_patterns and repos:
        print(f'\n{bold("git_ignored:")}')
        for repo_path, tags in repos:
            if not repo_path.exists():
                continue
            ignored = git_ls_ignored(repo_path, ignored_patterns)
            filtered = [f for f in ignored if not matches_exclude(str(f), excludes)]
            if not filtered:
                continue
            survey = survey_files(filtered, max_size_mb)
            merge_survey(manifest, survey)
            manifest['groups'].append(
                {'kind': 'git_ignored', 'source': str(repo_path), 'tags': tags, 'files': survey['files'], 'bytes': survey['bytes']}
            )
            copyable = [f for f in filtered if snapshot_rel(f) not in {s['path'].lstrip('/') for s in survey['skipped_large']}]
            rsync_untracked(copyable, dest_base, args.dry_run)
            label = yellow('would copy') if args.dry_run else green('copied')
            print(f'  {label} {bold(plural(survey["files"], "ignored file"))} from {cyan(str(repo_path))}')

    total_files = sum(g['files'] for g in manifest['groups'])
    total_bytes = sum(g['bytes'] for g in manifest['groups'])

    if not args.dry_run:
        dest_base.mkdir(parents=True, exist_ok=True)
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
    label = yellow('would back up') if args.dry_run else green('backed up')
    summary = f'{bold(plural(total_files, "file"))} ({bold(human_size(total_bytes))})'
    print(f'\n{bold("safekeep:")} {label} {summary} to {cyan(str(dest_base))} in {bold(elapsed_str)}')


def init_config(name):
    """Generate an example config file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config_path = CONFIG_DIR / f'{name}.toml'

    if config_path.exists():
        print(f'{yellow("safekeep:")} config already exists: {cyan(str(config_path))}', file=sys.stderr)
        print(f'  use {cyan("safekeep config show")} to view it, or edit directly', file=sys.stderr)
        sys.exit(1)

    config_path.write_text(CONFIG_TEMPLATE)

    print(f'{green("safekeep:")} created {cyan(str(config_path))}')
    print()
    print('  Edit the file to match your backup needs, then preview with:')
    print(f'    {cyan(f"safekeep --config {name} backup --dry-run")}')
    print()
    print('  Config format reference:')
    print(f'    {cyan("safekeep --help")}')


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


def show_config(config_path, config, warnings):
    """Display the resolved config with readable formatting."""
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


def show_help():
    help_header('safekeep', 'Dated snapshots of the files no package manager will put back.')
    help_usage('safekeep <command> [OPTIONS]')

    help_section('Commands')
    help_row('safekeep backup', '[--tag <name>]', "Copy the configured paths into today's snapshot")
    help_row('safekeep snapshots', '', 'List the snapshots at the destination')
    help_row('safekeep tags', '[name]', 'What each tag covers, and what it would restore')
    help_row('safekeep restore', '--to <path>', 'Restore groups from a snapshot')
    help_row('safekeep config', '<verb>', 'Inspect and create config files')
    help_row('safekeep update', '', 'Install the newest release')

    help_section('Options')
    help_row('-c, --config', '<name|path>', 'Config to use (default: auto-detect)')
    help_row('-V, --version', '', 'Print the running version')
    help_row('-h, --help', '', 'Show this help')
    help_text(
        '  Both go before the command: safekeep -c work backup',
        '  -n, --dry-run goes after it, on backup and restore: safekeep backup -n',
    )

    help_section('Config')
    help_text(
        '  Files live in ~/.config/safekeep/<name>.toml, one per backup destination.',
        '  `safekeep config example` prints an annotated config explaining every key.',
    )

    help_section('Restoring')
    help_text(
        '  Selection is always explicit — a restore never guesses at --all.',
        '  Rehearse into a scratch directory before restoring over anything real:',
        '      safekeep restore --to /tmp/restore-test --all',
        '  A snapshot carries the tags its config had that day, so `safekeep tags`',
        '  is what says whether --tag will select anything in it.',
    )

    help_section('Examples')
    help_row('safekeep config init', '', 'Write ~/.config/safekeep/default.toml')
    help_row('safekeep backup -n', '', 'See what a backup would copy')
    help_row('safekeep snapshots', '', 'What is on the destination already')
    help_row('safekeep tags secrets', '', 'What that tag would bring back')
    help_row('safekeep restore --to / --tag secrets', '', 'Restore one tag for real')

    help_end()


def show_backup_help():
    help_header('safekeep backup', "Copy the configured paths into today's snapshot.")
    help_usage('safekeep backup [OPTIONS]')

    help_section('Selection')
    help_text('  Everything the config lists, unless one of these narrows it:')
    help_row('--tag', '<name>', 'Only entries carrying NAME (repeatable)')
    help_row('--group', '<path>', 'Only entries whose path contains PATH (repeatable)')
    help_text(
        "  A narrowed run merges into the day's snapshot rather than replacing it, so the",
        '  groups it did not cover stay recorded and restorable.',
        '  safekeep tags is what says which names there are to narrow by.',
    )

    help_section('Options')
    help_row('-n, --dry-run', '', 'Show what would be copied, write nothing')
    help_row('-h, --help', '', 'Show this help')

    help_section('Examples')
    help_row('safekeep backup', '', 'Everything the config lists')
    help_row('safekeep backup -n', '', 'What a backup would copy, before it copies it')
    help_row('safekeep backup --tag secrets', '', 'Just the secrets, before doing something risky')
    help_row('safekeep backup --group ~/notes', '', 'One path, without walking the rest')

    help_end()


def show_restore_help():
    help_header('safekeep restore', 'Restore groups from a snapshot.')
    help_usage('safekeep restore --to <path> <selection> [OPTIONS]')

    help_section('Selection')
    help_text('  Required, and never inferred. Pass at least one:')
    help_row('--all', '', 'Every group in the snapshot')
    help_row('--group', '<path>', 'Groups whose source contains PATH (repeatable)')
    help_row('--tag', '<name>', 'Groups carrying NAME (repeatable)')
    help_text(
        '  A tag selects on the snapshot, which carries the tags its config had that day.',
        '  `safekeep tags <name>` is what says whether this one selects anything.',
    )

    help_section('Options')
    help_row('--to', '<path>', 'Restore target root (/ for a real restore)')
    help_row('--from', '<date>', 'Snapshot to restore from (default: pick, else newest)')
    help_row('--on-conflict', '<mode>', 'backup (default), skip, overwrite, newer')
    help_row('--skip-symlinked', '', 'Skip paths that were symlinks when backed up')
    help_row('-n, --dry-run', '', 'Show what would be restored, change nothing')
    help_row('-h, --help', '', 'Show this help')

    help_section('Examples')
    help_row('safekeep restore --to /tmp/rehearsal --all', '', 'Rehearse first — always')
    help_row('safekeep restore --to / --tag secrets', '', 'Restore one tag for real')
    help_row('safekeep restore --to / --from 2026-07-01 --all', '', 'Restore an older snapshot')

    help_end()


def show_config_help():
    help_header('safekeep config', 'Inspect and create config files.')
    help_usage('safekeep config <verb>')

    help_section('Commands')
    help_row('safekeep config show', '', 'Display the resolved config and exit')
    help_row('safekeep config edit', '', 'Open the config in $VISUAL or $EDITOR, then check it')
    help_row('safekeep config init', '[name]', "Write a starter config (default: 'default')")
    help_row('safekeep config example', '', 'Print the annotated example without writing it')

    help_section('Files')
    help_text(
        f'  {CONFIG_DIR}/<name>.toml',
        '  The annotated example is the key reference — it explains every key inline.',
    )

    help_end()


def build_parser():
    """Parsing only. Every screen's text lives in the show_*_help functions above, so
    argparse carries no help= strings that would be a second copy able to drift."""
    parser = argparse.ArgumentParser(prog='safekeep', add_help=False)
    parser.add_argument('-h', '--help', action='store_true', dest='show_help')
    parser.add_argument('-V', '--version', action='store_true', dest='show_version')
    parser.add_argument('-c', '--config')
    commands = parser.add_subparsers(dest='command', metavar='COMMAND')

    update_cmd = commands.add_parser('update', add_help=False)
    update_cmd.add_argument('-h', '--help', action='store_true', dest='show_help')

    backup = commands.add_parser('backup', add_help=False)
    backup.add_argument('-h', '--help', action='store_true', dest='show_help')
    backup.add_argument('--tag', action='append', default=[], metavar='NAME')
    backup.add_argument('--group', action='append', default=[], metavar='PATH')
    backup.add_argument('-n', '--dry-run', action='store_true')

    snapshots = commands.add_parser('snapshots', add_help=False)
    snapshots.add_argument('-h', '--help', action='store_true', dest='show_help')

    tags = commands.add_parser('tags', add_help=False)
    tags.add_argument('-h', '--help', action='store_true', dest='show_help')
    tags.add_argument('name', nargs='?', metavar='NAME')
    tags.add_argument('--from', dest='from_date', metavar='DATE')

    restore = commands.add_parser('restore', add_help=False)
    restore.add_argument('-h', '--help', action='store_true', dest='show_help')
    # Not required=True: argparse would reject `safekeep restore --help` for the missing
    # --to before there was any chance to show the screen that explains it. main() checks
    # for it after help routing instead.
    restore.add_argument('--to', metavar='PATH')
    restore.add_argument('--from', dest='from_date', metavar='DATE')
    restore.add_argument('--all', action='store_true')
    restore.add_argument('--group', action='append', default=[], metavar='PATH')
    restore.add_argument('--tag', action='append', default=[], metavar='NAME')
    restore.add_argument('--on-conflict', choices=['backup', 'skip', 'overwrite', 'newer'], default='backup')
    restore.add_argument('--skip-symlinked', action='store_true')
    restore.add_argument('-n', '--dry-run', action='store_true')

    # 'config' is a namespace, not a command. A bare `config` that printed would occupy the
    # noun slot with a verb's job, leaving nowhere for `config init` to go -- see
    # ~/dev/standards/cli-design.md, "A resource that could ever grow a second command is a
    # namespace today".
    config = commands.add_parser('config', add_help=False)
    config.add_argument('-h', '--help', action='store_true', dest='show_help')
    config_commands = config.add_subparsers(dest='config_command', metavar='COMMAND')
    config_show = config_commands.add_parser('show', add_help=False)
    config_show.add_argument('-h', '--help', action='store_true', dest='show_help')
    config_init = config_commands.add_parser('init', add_help=False)
    config_init.add_argument('-h', '--help', action='store_true', dest='show_help')
    config_init.add_argument('name', nargs='?', default='default')
    config_example = config_commands.add_parser('example', add_help=False)
    config_example.add_argument('-h', '--help', action='store_true', dest='show_help')
    config_edit = config_commands.add_parser('edit', add_help=False)
    config_edit.add_argument('-h', '--help', action='store_true', dest='show_help')

    # Undocumented on purpose: this renders a snapshot's manifest for the picker, so it is
    # machine plumbing rather than a verb anyone types.
    preview = commands.add_parser('preview-snapshot', add_help=False)
    preview.add_argument('date', metavar='DATE')

    # Kept so main() can tell a command typed alone from one that stated its intent and omitted
    # an option -- the first shows help, the second gets an error naming what is missing.
    parser.subcommands = commands.choices

    return parser


def screen_for(args):
    """The help screen covering whatever the reader has typed so far.

    Only the commands with a surface worth its own screen get one. `snapshots` and `tags` fall
    back to the root, which already documents them in full — a screen per flag would be a page
    you have to read to learn there was nothing on it.
    """
    if args.command == 'backup':
        return show_backup_help
    if args.command == 'restore':
        return show_restore_help
    if args.command == 'config':
        return show_config_help
    return show_help


def typed_alone(parser, args):
    """True when the command was typed with nothing after it.

    Compared against what the subparser produces from an empty command line rather than
    against a list of its options, which would fall out of date the first time one was added.
    """
    defaults = vars(parser.subcommands[args.command].parse_args([]))
    return all(getattr(args, dest, None) == value for dest, value in defaults.items())


def main():
    parser = build_parser()
    args = parser.parse_args()

    if getattr(args, 'show_version', False):
        print(f'safekeep {tool_version()}')
        sys.exit(0)

    if args.command == 'update' and not getattr(args, 'show_help', False):
        try:
            result = update(UPDATE_CONFIG)
        except SelfUpdateError as error:
            # Errors surface only here. The notice path swallows them into the
            # state file by design, so this is the one place they can be seen.
            print(f'{red("safekeep:")} {error}', file=sys.stderr)
            sys.exit(1)
        if result.applied:
            print(f'{green("safekeep")} updated {result.current} → {cyan(result.latest)}')
        else:
            print(f'safekeep is already at {cyan(result.current)}')
        sys.exit(0)

    # Deferred: pyselfupdate registers an atexit hook, so the one-line notice
    # lands after this command's own output rather than on top of it.
    notify(UPDATE_CONFIG)

    if getattr(args, 'show_help', False):
        # An explicit --help is a satisfied request, so it exits 0 where a bare or
        # incomplete invocation exits 2.
        screen_for(args)()
        sys.exit(0)

    if args.command is None:
        # Exit 2, matching every Typer tool on the fleet (no_args_is_help) and the usage-error
        # code in cli-design.md.
        show_help()
        sys.exit(2)

    if args.command == 'restore' and typed_alone(parser, args):
        # No args shows help, always -- an incomplete command line is answered with the screen
        # that completes it, never with an error. `restore --all` with --to forgotten is the
        # other case: intent was stated, so the error below names the one thing missing.
        show_restore_help()
        sys.exit(2)

    if args.command == 'restore' and not args.to:
        # Checked here rather than by argparse so that `restore --help` reaches its screen;
        # see the --to argument in build_parser.
        print(f'\n  {red("restore needs --to")}: the root to restore into, or / for a real restore', file=sys.stderr)
        print(f'  Rehearse first: {cyan("safekeep restore --to /tmp/restore-test --all")}\n', file=sys.stderr)
        sys.exit(2)

    if args.command == 'config':
        # A bare `safekeep config` selects nothing, so it shows help and exits 2 like a bare
        # invocation does, rather than guessing that 'show' was meant.
        if args.config_command is None:
            show_config_help()
            sys.exit(2)
        if args.config_command == 'init':
            init_config(args.config or args.name)
            sys.exit(0)
        if args.config_command == 'example':
            print(CONFIG_TEMPLATE, end='')
            sys.exit(0)
        if args.config_command == 'edit':
            # Resolved but deliberately not loaded: a config that no longer loads is the main
            # reason to open one, and load_config exits before the editor could fix it.
            edit_config(resolve_config(args.config))
            sys.exit(0)

    config_path = resolve_config(args.config)
    config, warnings = load_config(config_path)

    if args.command == 'preview-snapshot':
        preview_snapshot(Path(config['back_up_to']).expanduser(), args.date)
    elif args.command == 'config':
        show_config(config_path, config, warnings)
    elif args.command == 'snapshots':
        show_snapshots(Path(config['back_up_to']).expanduser())
    elif args.command == 'tags':
        show_tags(config, config_path, args)
    elif args.command == 'restore':
        do_restore(config, config_path, args)
    elif args.command == 'backup':
        do_backup(config, config_path, warnings, args)
