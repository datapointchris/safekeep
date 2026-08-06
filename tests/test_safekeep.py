"""Tests for safekeep — config leniency, manifest contents, and restore fidelity.

safekeep is an installed package here, so it imports normally; the subprocess
tests invoke it with `python -m safekeep`, which is the closest thing to how a
user runs it without depending on the console script being on PATH. The restore
path is destructive, so the tests that matter most are the round-trips: back up a
fixture tree to a temp dest, restore it to a second temp dir, and assert that modes
survived the trip. The destination in real use is SMB and cannot store modes, which is
the whole reason the manifest records them — these tests stand in for that by asserting
against the manifest rather than against the copied tree's own permissions.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
import tomli_w

import safekeep


def write_config(tmp_path, dest, **extra):
    """Write a TOML config. tomli_w is a dev dependency only — safekeep reads with stdlib
    tomllib and never writes a config, so nothing it ships depends on this."""
    config = {'back_up_to': str(dest), **extra}
    config_path = tmp_path / 'test.toml'
    config_path.write_text(tomli_w.dumps(config))
    return config_path


def paths(*entries):
    """[[back_up_paths]] blocks — every entry is a table with a 'path'."""
    return [{'path': str(entry)} for entry in entries]


ANSI = re.compile(r'\x1b\[[0-9;]*m')


def run_safekeep(*args, env=None):
    """Invoke the script as a subprocess, the way a user does."""
    return subprocess.run([sys.executable, '-m', 'safekeep', *args], capture_output=True, text=True, env=env)


def editor_writing(tmp_path, content):
    """A stand-in $EDITOR: a script that replaces the file it is handed with `content`."""
    script = tmp_path / 'fake-editor.py'
    script.write_text(f'import pathlib\nimport sys\n\npathlib.Path(sys.argv[1]).write_text({content!r})\n')
    return {**os.environ, 'EDITOR': f'{sys.executable} {script}'}


def plain(text):
    return ANSI.sub('', text)


@pytest.fixture
def source_tree(tmp_path):
    """A source tree carrying every shape the tool branches on.

    Restore branches on directory vs single file, and again on whether the source itself was a
    symlink; backup branches on the mode deviations worth recording and on what the excludes
    drop. All three shapes are groups in `matrix_config` below, so the restore tests cross the
    whole matrix instead of one corner of it — which is what a single-directory fixture gave
    for a long time, leaving restore_group's file branch with no caller at all.
    """
    src = tmp_path / 'src'
    (src / 'notes').mkdir(parents=True)
    (src / 'notes' / 'plain.md').write_text('plain\n')
    (src / 'notes' / 'secret.txt').write_text('secret\n')
    (src / 'notes' / 'run.sh').write_text('#!/bin/sh\necho hi\n')
    (src / 'notes' / '.venv').mkdir()
    (src / 'notes' / '.venv' / 'junk').write_text('junk\n')

    (src / 'notes' / 'plain.md').chmod(0o644)
    (src / 'notes' / 'secret.txt').chmod(0o600)
    (src / 'notes' / 'run.sh').chmod(0o755)

    # A single file as its own group: the shape the shipped config template leads with
    # (`~/.gitconfig`), and the one that takes restore's non-directory branch.
    (src / 'solo.conf').write_text('solo\n')
    (src / 'solo.conf').chmod(0o600)

    (src / 'real.conf').write_text('real\n')
    (src / 'linked.conf').symlink_to(src / 'real.conf')
    return src


def matrix_config(tmp_path, dest, source_tree, **extra):
    """The config the restore tests share: one group of each shape restore branches on."""
    return write_config(
        tmp_path,
        dest,
        back_up_paths=[
            {'path': str(source_tree / 'notes'), 'tags': ['docs']},
            {'path': str(source_tree / 'solo.conf'), 'tags': ['docs', 'secrets']},
            {'path': str(source_tree / 'linked.conf'), 'tags': ['docs']},
        ],
        **extra,
    )


# --- command surface ------------------------------------------------------------------


def test_bare_invocation_shows_help_and_writes_nothing(tmp_path, source_tree):
    """A tool with subcommands never does work bare — see cli-design.md, 'No args shows help'."""
    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
    result = run_safekeep('--config', str(config_path))
    assert result.returncode == 2  # usage error, matching every Typer tool's no_args_is_help
    assert 'Usage: safekeep' in result.stdout
    assert not dest.exists()


def test_explicit_help_is_a_satisfied_request(tmp_path):
    """`--help` asked and got an answer; a bare invocation is an incomplete command line."""
    assert run_safekeep('--help').returncode == 0


def test_help_lists_every_public_command(tmp_path):
    result = run_safekeep('--help')
    for command in ('backup', 'snapshots', 'tags', 'restore', 'config'):
        assert command in result.stdout


def test_restore_help_works_without_the_option_it_documents(tmp_path):
    """--to cannot be argparse-required, or asking how to use restore fails on the very
    argument the answer explains. See the --to argument in build_parser."""
    result = run_safekeep('restore', '--help')
    assert result.returncode == 0
    assert '--to' in result.stdout
    for selection in ('--all', '--group', '--tag'):
        assert selection in result.stdout


def test_restore_without_a_target_says_which_option_is_missing(tmp_path):
    result = run_safekeep('restore', '--all')
    assert result.returncode == 2
    assert '--to' in result.stderr
    assert 'restore-test' in result.stderr, 'the error points at rehearsing, not just at the flag'


def test_backup_help_documents_narrowing_a_run(tmp_path):
    result = run_safekeep('backup', '--help')
    assert result.returncode == 0
    assert '--tag' in result.stdout
    assert 'merges' in result.stdout, 'a narrowed run rewrites a manifest, which needs saying'


def test_restore_help_is_its_own_screen_not_the_root(tmp_path):
    result = run_safekeep('restore', '--help')
    assert 'safekeep restore' in result.stdout
    assert 'safekeep backup' not in result.stdout, 'a drill-down screen describes what was asked about'


def test_config_is_a_namespace_and_a_bare_one_shows_its_own_help(tmp_path):
    """`config` names a resource; selecting nothing under it is an incomplete command line."""
    result = run_safekeep('config')
    assert result.returncode == 2
    for command in ('show', 'edit', 'init', 'example'):
        assert command in result.stdout


def test_config_example_prints_the_template_without_needing_a_config(tmp_path, monkeypatch):
    """Reading the annotated example is what you do *before* you have a config."""
    monkeypatch.setenv('HOME', str(tmp_path))
    result = subprocess.run([sys.executable, '-m', 'safekeep', 'config', 'example'], capture_output=True, text=True, env=os.environ)
    assert result.returncode == 0
    assert result.stdout == safekeep.CONFIG_TEMPLATE
    assert not (tmp_path / '.config' / 'safekeep').exists()


def test_bare_restore_shows_help_rather_than_an_error(tmp_path):
    """No args shows help, always — an incomplete command line gets the screen that completes
    it. `restore --all` with --to forgotten is the other case, tested below."""
    result = run_safekeep('restore')
    assert result.returncode == 2
    assert 'safekeep restore' in result.stdout
    assert result.stderr == ''


def test_unknown_command_is_a_usage_error(tmp_path):
    result = run_safekeep('bakup')
    assert result.returncode == 2


def test_help_hides_the_fzf_preview_helper(tmp_path):
    """preview-snapshot exists only to feed fzf's preview pane; it is not user surface."""
    result = run_safekeep('--help')
    assert 'preview-snapshot' not in result.stdout
    assert 'SUPPRESS' not in result.stdout


# --- config loading -------------------------------------------------------------------


def test_missing_required_key_is_fatal(tmp_path):
    config_path = tmp_path / 'bad.toml'
    config_path.write_text('back_up_paths = []\n')
    with pytest.raises(SystemExit) as exc:
        safekeep.load_config(config_path)
    assert exc.value.code == 1


def test_invalid_toml_names_the_file_and_the_parse_error(tmp_path):
    config_path = tmp_path / 'bad.toml'
    config_path.write_text('back_up_to = \n')
    with pytest.raises(SystemExit) as exc:
        safekeep.load_config(config_path)
    assert exc.value.code == 1


def test_unknown_key_warns_and_loads(tmp_path):
    config_path = write_config(tmp_path, tmp_path / 'dest', pathz=['~/typo'])
    config, warnings = safekeep.load_config(config_path)
    assert config['back_up_to'] == str(tmp_path / 'dest')
    assert any('pathz' in w and 'unrecognized' in w for w in warnings)


def test_retired_key_carries_its_own_message(tmp_path):
    config_path = write_config(tmp_path, tmp_path / 'dest', keep=7)
    _, warnings = safekeep.load_config(config_path)
    assert any(w.startswith('keep:') and 'retention was removed' in w for w in warnings)


def test_renamed_key_is_fatal_rather_than_warned(tmp_path):
    """A warning would let the run continue and quietly back up no repos at all."""
    config_path = write_config(tmp_path, tmp_path / 'dest', git_untracked=['~/code/thing'])
    with pytest.raises(SystemExit) as exc:
        safekeep.load_config(config_path)
    assert exc.value.code == 1


def test_a_wholly_old_config_reports_the_renames_not_the_missing_key(tmp_path, capsys):
    """Every key changed, so 'back_up_to is missing' would be the least useful thing to say."""
    config_path = tmp_path / 'old.toml'
    config_path.write_text('dest = "/mnt/h"\npaths = ["~/notes"]\nmax_file_size_mb = 50\n')
    with pytest.raises(SystemExit):
        safekeep.load_config(config_path)
    err = plain(capsys.readouterr().err)
    assert 'was renamed' in err
    assert 'missing required key' not in err


def test_repo_options_without_repos_warn_that_they_do_nothing(tmp_path):
    config_path = write_config(tmp_path, tmp_path / 'dest', git={'back_up_ignored_files_matching': ['CLAUDE.md']})
    _, warnings = safekeep.load_config(config_path)
    assert any('back_up_ignored_files_matching' in w and 'does nothing' in w for w in warnings)


def test_unknown_repo_subkey_warns(tmp_path):
    config_path = write_config(tmp_path, tmp_path / 'dest', git={'repos': [], 'pathz': []})
    _, warnings = safekeep.load_config(config_path)
    assert any('git.pathz' in w and 'unrecognized' in w for w in warnings)


def test_repo_entries_reads_the_repos_and_what_to_take_from_them():
    repos, untracked, patterns = safekeep.repo_entries(
        {'git': {'repos': [{'path': '/a'}], 'back_up_ignored_files_matching': ['CLAUDE.md']}}
    )
    assert repos == [(Path('/a'), [])]
    assert untracked is True  # copying untracked files is the default the key documents
    assert patterns == ['CLAUDE.md']


def test_untracked_files_can_be_turned_off_leaving_only_the_ignored_patterns(tmp_path):
    config_path = write_config(
        tmp_path,
        tmp_path / 'dest',
        git={'repos': [{'path': '/a'}], 'back_up_untracked_files': False, 'back_up_ignored_files_matching': ['CLAUDE.md']},
    )
    config, _ = safekeep.load_config(config_path)
    _, untracked, patterns = safekeep.repo_entries(config)
    assert untracked is False
    assert patterns == ['CLAUDE.md']


def test_normalize_entries_reads_tables_with_optional_tags():
    entries = safekeep.normalize_entries([{'path': '/a'}, {'path': '/b', 'tags': ['windows']}])
    assert entries[0] == (Path('/a'), [])
    assert entries[1] == (Path('/b'), ['windows'])


def test_normalize_entries_rejects_a_table_without_path():
    with pytest.raises(SystemExit):
        safekeep.normalize_entries([{'tags': ['oops']}])


def test_normalize_entries_rejects_a_bare_string():
    """An array of tables has one shape; a bare path was the JSON schema's second one."""
    with pytest.raises(SystemExit):
        safekeep.normalize_entries(['/a'])


def test_the_shipped_template_parses_and_warns_about_nothing(tmp_path):
    """`init` writes this verbatim, so it is the first config anyone runs."""
    config_path = tmp_path / 'template.toml'
    config_path.write_text(safekeep.CONFIG_TEMPLATE)
    config, warnings = safekeep.load_config(config_path)
    assert warnings == []
    assert safekeep.normalize_entries(config['back_up_paths'])
    repos, untracked, patterns = safekeep.repo_entries(config)
    assert repos and untracked is True and patterns


def test_the_shipped_template_demonstrates_repetition_not_one_of_each(tmp_path):
    """A one-of-each example teaches that one is the expected count — see documentation.md."""
    config_path = tmp_path / 'template.toml'
    config_path.write_text(safekeep.CONFIG_TEMPLATE)
    config, _ = safekeep.load_config(config_path)
    entries = safekeep.normalize_entries(config['back_up_paths'])
    repos, _, patterns = safekeep.repo_entries(config)
    assert len(entries) > 1
    assert len(repos) > 1
    assert len(patterns) > 1
    tags = [tag for _, entry_tags in entries + repos for tag in entry_tags]
    assert len(tags) > len(set(tags))  # a tag reused across entries, which is how tags are used


def test_leftover_json_configs_are_named_rather_than_reported_as_absent(tmp_path, monkeypatch, capsys):
    """'no configs found' is bewildering when the config file is sitting right there."""
    config_dir = tmp_path / 'safekeep'
    config_dir.mkdir()
    (config_dir / 'work.json').write_text('{}')
    monkeypatch.setattr(safekeep, 'CONFIG_DIR', config_dir)
    with pytest.raises(SystemExit):
        safekeep.resolve_config(None)
    err = plain(capsys.readouterr().err)
    assert 'configs are TOML now' in err
    assert 'work.json -> work.toml' in err


# --- surveying ------------------------------------------------------------------------


def test_survey_records_only_mode_deviations(source_tree):
    survey = safekeep.survey_tree(source_tree / 'notes', safekeep.DEFAULT_SKIP_NAMES, None)
    modes = survey['modes']
    assert safekeep.snapshot_rel(source_tree / 'notes' / 'secret.txt') in modes
    assert safekeep.snapshot_rel(source_tree / 'notes' / 'run.sh') in modes
    assert safekeep.snapshot_rel(source_tree / 'notes' / 'plain.md') not in modes


def test_survey_honours_excludes(source_tree):
    survey = safekeep.survey_tree(source_tree / 'notes', safekeep.DEFAULT_SKIP_NAMES, None)
    assert not any('.venv' in key for key in survey['modes'])
    assert survey['files'] == 3


def test_survey_records_symlink_targets(source_tree):
    survey = safekeep.survey_tree(source_tree / 'linked.conf', safekeep.DEFAULT_SKIP_NAMES, None)
    assert survey['symlinks'][safekeep.snapshot_rel(source_tree / 'linked.conf')] == str(source_tree / 'real.conf')


def test_survey_skips_oversized_files(tmp_path):
    big = tmp_path / 'big.bin'
    big.write_bytes(b'x' * (2 * 1024 * 1024))
    survey = safekeep.survey_tree(big, safekeep.DEFAULT_SKIP_NAMES, 1)
    assert survey['files'] == 0
    assert survey['skipped_large'][0]['path'] == str(big)


def test_survey_survives_symlink_cycle(tmp_path):
    root = tmp_path / 'loop'
    (root / 'inner').mkdir(parents=True)
    (root / 'inner' / 'file.txt').write_text('x\n')
    (root / 'inner' / 'back').symlink_to(root)
    survey = safekeep.survey_tree(root, safekeep.DEFAULT_SKIP_NAMES, None)
    assert survey['files'] >= 1


# --- backup ---------------------------------------------------------------------------


def test_backup_writes_manifest_with_groups_and_tags(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, back_up_paths=[{'path': str(source_tree / 'notes'), 'tags': ['docs']}])
    result = run_safekeep('--config', str(config_path), 'backup')
    assert result.returncode == 0, result.stderr

    snapshot = next(d for d in dest.iterdir() if d.is_dir())
    manifest = json.loads((snapshot / safekeep.MANIFEST_NAME).read_text())

    assert manifest['version'] == safekeep.MANIFEST_VERSION
    assert manifest['home'] == str(Path.home())
    # Which build wrote this, beside which format it wrote. A snapshot outlives
    # the machine, so a future format change has to be attributable to a release
    # rather than guessed at from the shape of the file.
    assert manifest['safekeep_version'] == safekeep.tool_version()
    group = manifest['groups'][0]
    assert group['kind'] == 'path'
    assert group['source'] == str(source_tree / 'notes')
    assert group['tags'] == ['docs']
    assert group['files'] == 3


def test_backup_narrows_to_the_sources_a_tag_covers(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = tagged(tmp_path, dest, (source_tree / 'notes', ['docs']), (source_tree / 'real.conf', ['conf']))

    result = run_safekeep('--config', str(config_path), 'backup', '--tag', 'conf')
    assert result.returncode == 0, result.stderr

    snapshot = next(d for d in dest.iterdir() if d.is_dir())
    manifest = json.loads((snapshot / safekeep.MANIFEST_NAME).read_text())
    assert [group['source'] for group in manifest['groups']] == [str(source_tree / 'real.conf')]


def test_a_narrowed_backup_keeps_the_groups_it_did_not_cover(tmp_path, source_tree):
    """rsync never deletes, so the untouched files are still in the snapshot — dropping their
    groups would leave them on disk and unrestorable, since the manifest is the only record."""
    dest = tmp_path / 'dest'
    config_path = tagged(tmp_path, dest, (source_tree / 'notes', ['docs']), (source_tree / 'real.conf', ['conf']))
    run_safekeep('--config', str(config_path), 'backup')
    run_safekeep('--config', str(config_path), 'backup', '--tag', 'conf')

    snapshot = next(d for d in dest.iterdir() if d.is_dir())
    manifest = json.loads((snapshot / safekeep.MANIFEST_NAME).read_text())
    assert {group['source'] for group in manifest['groups']} == {str(source_tree / 'notes'), str(source_tree / 'real.conf')}
    assert manifest['modes'], 'the modes recorded for the untouched group survive the merge too'

    target = tmp_path / 'target'
    restore = run_safekeep('--config', str(config_path), 'restore', '--to', str(target), '--tag', 'docs')
    assert restore.returncode == 0, restore.stderr
    assert (target / safekeep.snapshot_rel(source_tree / 'notes') / 'plain.md').exists()


def test_backup_by_unknown_tag_is_a_usage_error(tmp_path, source_tree):
    """Backing up nothing reads exactly like backing up everything asked for, so a typo has to
    fail rather than succeed at covering nothing."""
    dest = tmp_path / 'dest'
    config_path = tagged(tmp_path, dest, (source_tree / 'notes', ['docs']))

    result = run_safekeep('--config', str(config_path), 'backup', '--tag', 'nope')
    assert result.returncode == 2
    assert 'docs' in result.stderr
    assert not dest.exists()


def test_backup_by_unknown_group_lists_the_paths_there_are(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = tagged(tmp_path, dest, (source_tree / 'notes', ['docs']))

    result = run_safekeep('--config', str(config_path), 'backup', '--group', 'zzz')
    assert result.returncode == 2
    assert str(source_tree / 'notes') in plain(result.stderr)


def test_backup_skips_a_path_that_does_not_exist_here(tmp_path, source_tree):
    """One config is shared across machines, so entries that exist on WSL and not on macOS are
    the normal case rather than an error. The run continues, says which path it skipped, and
    records no group for it — a group with no files would read as a backup that took nothing."""
    dest = tmp_path / 'dest'
    config_path = write_config(
        tmp_path,
        dest,
        back_up_paths=[{'path': str(source_tree / 'notes')}, {'path': '/mnt/c/Users/nobody/Documents'}],
    )
    result = run_safekeep('--config', str(config_path), 'backup')
    assert result.returncode == 0, result.stderr
    assert 'not found' in plain(result.stdout)
    assert '/mnt/c/Users/nobody/Documents' in plain(result.stdout)

    snapshot = next(d for d in dest.iterdir() if d.is_dir())
    manifest = json.loads((snapshot / safekeep.MANIFEST_NAME).read_text())
    assert [group['source'] for group in manifest['groups']] == [str(source_tree / 'notes')]


def test_backup_skips_a_git_repo_that_does_not_exist_here(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = write_config(
        tmp_path,
        dest,
        back_up_paths=paths(source_tree / 'notes'),
        git={'repos': [{'path': str(tmp_path / 'no-such-repo')}], 'back_up_ignored_files_matching': ['*.env']},
    )
    result = run_safekeep('--config', str(config_path), 'backup')
    assert result.returncode == 0, result.stderr
    assert 'not found' in plain(result.stdout)

    snapshot = next(d for d in dest.iterdir() if d.is_dir())
    manifest = json.loads((snapshot / safekeep.MANIFEST_NAME).read_text())
    assert {group['kind'] for group in manifest['groups']} == {'path'}


def test_backup_warns_when_a_listed_repo_is_not_a_git_repo(tmp_path, source_tree):
    """A directory that exists but was never `git init`ed: git fails, and the run has to say so
    rather than record an empty group that looks like a repo with nothing untracked."""
    dest = tmp_path / 'dest'
    not_a_repo = tmp_path / 'not-a-repo'
    not_a_repo.mkdir()
    (not_a_repo / 'file.txt').write_text('x\n')
    config_path = write_config(tmp_path, dest, git={'repos': [{'path': str(not_a_repo)}]})

    result = run_safekeep('--config', str(config_path), 'backup')
    assert result.returncode == 0, result.stderr
    assert 'could not list untracked files' in plain(result.stdout)


def test_backup_names_every_file_it_skipped_for_size(tmp_path, source_tree):
    """A short backup is never silently short: the oversized file stays out of the snapshot and
    is named in the manifest, so a restore that comes up missing has a written reason."""
    dest = tmp_path / 'dest'
    big = source_tree / 'notes' / 'big.bin'
    big.write_bytes(b'0' * (2 * 1024 * 1024))
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'), skip_files_over_mb=1)

    result = run_safekeep('--config', str(config_path), 'backup')
    assert result.returncode == 0, result.stderr
    assert 'skipped 1 file over 1 MB' in plain(result.stdout)

    snapshot = next(d for d in dest.iterdir() if d.is_dir())
    manifest = json.loads((snapshot / safekeep.MANIFEST_NAME).read_text())
    assert [entry['path'] for entry in manifest['skipped_large']] == [str(big)]
    assert not (snapshot / safekeep.snapshot_rel(big)).exists()
    assert (snapshot / safekeep.snapshot_rel(source_tree / 'notes' / 'plain.md')).exists()


@pytest.mark.skipif(os.geteuid() == 0, reason='root writes to a read-only directory anyway')
def test_backup_fails_fast_on_an_unwritable_destination(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    dest.mkdir()
    dest.chmod(0o555)
    try:
        config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
        result = run_safekeep('--config', str(config_path), 'backup')
        assert result.returncode == 1
        assert 'not writable' in result.stderr
        assert str(dest) in plain(result.stderr)
    finally:
        dest.chmod(0o755)


def test_backup_records_config_warnings_in_manifest(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'), keep=5)
    run_safekeep('--config', str(config_path), 'backup')

    snapshot = next(d for d in dest.iterdir() if d.is_dir())
    manifest = json.loads((snapshot / safekeep.MANIFEST_NAME).read_text())
    assert any('keep' in w for w in manifest['config_warnings'])


def test_dry_run_writes_nothing(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
    run_safekeep('--config', str(config_path), 'backup', '--dry-run')
    assert not any(dest.iterdir())


def test_backup_does_not_prune_old_snapshots(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    dest.mkdir()
    for old in ('2020-01-01', '2020-01-02', '2020-01-03'):
        (dest / old).mkdir()
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
    run_safekeep('--config', str(config_path), 'backup')
    assert (dest / '2020-01-01').exists()
    assert (dest / '2020-01-03').exists()


def git(*args: str, cwd: Path) -> None:
    """Run git in a throwaway repo with the ambient git environment scrubbed.

    Inheriting it makes these tests pass standalone and fail from inside a hook:
    pre-commit exports GIT_INDEX_FILE, so a subprocess here writes to the *outer*
    repo's index and then cannot build a tree, because the objects that index
    names do not exist in the temp repo.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    subprocess.run(['git', *args], cwd=cwd, check=True, env=env)


def git_commit_all(cwd: Path, *files: str) -> None:
    """Stage the named files and make the initial commit, identity supplied."""
    git('init', '-q', cwd=cwd)
    git('add', *files, cwd=cwd)
    git('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'init', cwd=cwd)


def test_git_untracked_becomes_its_own_group(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'tracked.txt').write_text('tracked\n')
    git_commit_all(repo, 'tracked.txt')
    (repo / 'wip.txt').write_text('wip\n')

    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, git={'repos': [{'path': str(repo), 'tags': ['wip']}]})
    result = run_safekeep('--config', str(config_path), 'backup')
    assert result.returncode == 0, result.stderr

    snapshot = next(d for d in dest.iterdir() if d.is_dir())
    manifest = json.loads((snapshot / safekeep.MANIFEST_NAME).read_text())
    group = manifest['groups'][0]
    assert group['kind'] == 'git_untracked'
    assert group['tags'] == ['wip']
    assert group['files'] == 1
    assert (snapshot / safekeep.snapshot_rel(repo / 'wip.txt')).exists()
    assert not (snapshot / safekeep.snapshot_rel(repo / 'tracked.txt')).exists()


# --- snapshots ------------------------------------------------------------------------


def test_list_snapshots_is_newest_first(tmp_path):
    dest = tmp_path / 'dest'
    for name in ('2026-01-01', '2026-03-03', '2026-02-02'):
        (dest / name).mkdir(parents=True)
    names = [d.name for d, _ in safekeep.list_snapshots(dest)]
    assert names == ['2026-03-03', '2026-02-02', '2026-01-01']


def test_snapshots_flags_manifestless_directories(tmp_path):
    dest = tmp_path / 'dest'
    (dest / '2026-01-01').mkdir(parents=True)
    config_path = write_config(tmp_path, dest)
    result = run_safekeep('--config', str(config_path), 'snapshots')
    assert 'no manifest' in result.stdout


# --- tags -----------------------------------------------------------------------------


def tagged(tmp_path, dest, *entries):
    """A config of (path, tags) entries, rewritten in place so a test can retag between runs."""
    config_path = tmp_path / 'test.toml'
    config = {'back_up_to': str(dest), 'back_up_paths': [{'path': str(path), 'tags': tags} for path, tags in entries]}
    config_path.write_text(tomli_w.dumps(config))
    return config_path


def test_tags_lists_each_tag_with_what_it_would_restore(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = tagged(tmp_path, dest, (source_tree / 'notes', ['docs', 'rebuild']))
    run_safekeep('--config', str(config_path), 'backup')

    result = run_safekeep('--config', str(config_path), 'tags')
    assert result.returncode == 0
    out = plain(result.stdout)
    assert '2 tags' in out
    assert 'docs' in out and 'rebuild' in out
    assert '3 files' in out, 'a tag is sized from the snapshot it would be restored from'


def test_tags_flags_a_tag_the_snapshot_predates(tmp_path, source_tree):
    """The failure this command exists for. Tagging an entry does not retag the snapshots that
    already exist, so `restore --tag` comes back empty while the config plainly carries the tag."""
    dest = tmp_path / 'dest'
    config_path = tagged(tmp_path, dest, (source_tree / 'notes', ['docs']))
    run_safekeep('--config', str(config_path), 'backup')

    tagged(tmp_path, dest, (source_tree / 'notes', ['docs']), (source_tree / 'real.conf', ['wsl']))
    out = plain(run_safekeep('--config', str(config_path), 'tags').stdout)
    assert 'wsl' in out
    assert 'not in this snapshot' in out


def test_tags_keeps_listing_a_tag_the_config_dropped(tmp_path, source_tree):
    """--tag selects on the snapshot, so a tag removed from the config is still the only name
    the snapshots taken before the removal answer to."""
    dest = tmp_path / 'dest'
    config_path = tagged(tmp_path, dest, (source_tree / 'notes', ['docs']))
    run_safekeep('--config', str(config_path), 'backup')

    tagged(tmp_path, dest, (source_tree / 'notes', ['rebuild']))
    assert 'docs' in plain(run_safekeep('--config', str(config_path), 'tags').stdout)
    detail = plain(run_safekeep('--config', str(config_path), 'tags', 'docs').stdout)
    assert 'tagged in the snapshot only' in detail


def test_tags_names_the_restore_that_would_bring_one_back(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = tagged(tmp_path, dest, (source_tree / 'notes', ['docs']))
    run_safekeep('--config', str(config_path), 'backup')

    out = plain(run_safekeep('--config', str(config_path), 'tags', 'docs').stdout)
    assert '--tag docs' in out
    assert str(source_tree / 'notes') in out


def test_tags_from_sizes_against_the_snapshot_named(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = tagged(tmp_path, dest, (source_tree / 'notes', ['docs']))
    run_safekeep('--config', str(config_path), 'backup')
    date = next(d.name for d, _ in safekeep.list_snapshots(dest))

    out = plain(run_safekeep('--config', str(config_path), 'tags', 'docs', '--from', date).stdout)
    assert f'--from {date}' in out, 'the restore it prints has to reach the snapshot it just sized'
    assert run_safekeep('--config', str(config_path), 'tags', '--from', '2020-01-01').returncode == 1


def test_unknown_tag_lists_the_ones_that_exist(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = tagged(tmp_path, dest, (source_tree / 'notes', ['docs']))
    result = run_safekeep('--config', str(config_path), 'tags', 'nope')
    assert result.returncode == 2  # usage error: a name that was never valid
    assert 'docs' in result.stderr


def test_tags_counts_the_sources_no_tag_reaches(tmp_path, source_tree):
    """An untagged source is restorable only with --all or --group, which is worth knowing
    before a rebuild rather than during one."""
    dest = tmp_path / 'dest'
    config_path = tagged(tmp_path, dest, (source_tree / 'notes', ['docs']), (source_tree / 'real.conf', []))
    out = plain(run_safekeep('--config', str(config_path), 'tags').stdout)
    assert 'untagged: 1 source' in out


def test_tags_works_before_the_first_backup(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = tagged(tmp_path, dest, (source_tree / 'notes', ['docs']))
    result = run_safekeep('--config', str(config_path), 'tags')
    assert result.returncode == 0
    assert 'no snapshots' in plain(result.stdout)


def test_a_bare_string_tag_is_fatal(tmp_path, source_tree):
    """tags = "wsl" is a list of characters to Python, and the only symptom would be a restore
    selecting nothing from a snapshot whose config plainly carries the tag."""
    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, back_up_paths=[{'path': str(source_tree / 'notes'), 'tags': 'wsl'}])
    result = run_safekeep('--config', str(config_path), 'tags')
    assert result.returncode == 1
    assert 'list of strings' in result.stderr


# --- config edit ----------------------------------------------------------------------


def test_config_edit_reports_what_the_edit_introduced(tmp_path, source_tree):
    """The moment to learn a key is retired is while the editor is still in hand."""
    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
    env = editor_writing(tmp_path, f'back_up_to = "{dest}"\nkeep = 5\n')

    result = run_safekeep('--config', str(config_path), 'config', 'edit', env=env)
    assert result.returncode == 0
    assert 'retention was removed' in plain(result.stdout)


def test_config_edit_opens_a_config_that_no_longer_loads(tmp_path):
    """A config that fails to load is the main reason to open one, so edit resolves the path
    without loading it — loading first would exit before the editor could fix anything."""
    config_path = tmp_path / 'test.toml'
    config_path.write_text('back_up_to =\n')
    fixed = 'back_up_to = "/tmp/somewhere"\n'

    result = run_safekeep('--config', str(config_path), 'config', 'edit', env=editor_writing(tmp_path, fixed))
    assert result.returncode == 0, result.stderr
    assert config_path.read_text() == fixed


def test_config_edit_reports_an_edit_that_broke_the_file(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))

    result = run_safekeep('--config', str(config_path), 'config', 'edit', env=editor_writing(tmp_path, 'back_up_to =\n'))
    assert result.returncode == 1
    assert 'not valid TOML' in result.stderr


def test_config_edit_without_an_editor_names_the_variables_and_the_file(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
    env = {key: value for key, value in os.environ.items() if key not in ('EDITOR', 'VISUAL')}

    result = run_safekeep('--config', str(config_path), 'config', 'edit', env=env)
    assert result.returncode == 1
    assert '$EDITOR' in result.stderr
    assert str(config_path) in plain(result.stderr), 'the fallback is editing it by hand'


# --- restore --------------------------------------------------------------------------


def backup_and_restore(tmp_path, source_tree, *restore_args, config_extra=None):
    """Back up all three group shapes, then restore. The target does not exist beforehand,
    which is how a rehearsal is actually run and is half of what the file branch trips on."""
    dest = tmp_path / 'dest'
    target = tmp_path / 'target'
    config_path = matrix_config(tmp_path, dest, source_tree, **(config_extra or {}))
    backup = run_safekeep('--config', str(config_path), 'backup')
    assert backup.returncode == 0, backup.stderr
    restore = run_safekeep('--config', str(config_path), 'restore', '--to', str(target), *restore_args)
    return restore, target


def test_restore_requires_to(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
    run_safekeep('--config', str(config_path), 'backup')
    result = run_safekeep('--config', str(config_path), 'restore', '--all')
    assert result.returncode == 2  # usage error, per cli-design.md
    assert '--to' in result.stderr


def test_restore_without_selection_is_an_error_when_not_a_tty(tmp_path, source_tree):
    restore, _ = backup_and_restore(tmp_path, source_tree)
    assert restore.returncode == 1
    assert 'no groups selected' in restore.stderr


def test_restore_all_reproduces_content_and_modes(tmp_path, source_tree):
    restore, target = backup_and_restore(tmp_path, source_tree, '--all')
    assert restore.returncode == 0, restore.stderr

    restored = target / safekeep.snapshot_rel(source_tree / 'notes')
    assert (restored / 'plain.md').read_text() == 'plain\n'
    assert stat.S_IMODE((restored / 'secret.txt').stat().st_mode) == 0o600
    assert stat.S_IMODE((restored / 'run.sh').stat().st_mode) == 0o755
    assert stat.S_IMODE((restored / 'plain.md').stat().st_mode) == 0o644

    solo = target / safekeep.snapshot_rel(source_tree / 'solo.conf')
    assert solo.read_text() == 'solo\n'
    assert stat.S_IMODE(solo.stat().st_mode) == 0o600

    linked = target / safekeep.snapshot_rel(source_tree / 'linked.conf')
    assert linked.is_file() and not linked.is_symlink(), 'symlinks are dereferenced on backup'
    assert linked.read_text() == 'real\n'


def flatten_modes(snapshot):
    """Strip mode information the way an SMB/DrvFs destination does.

    A local test destination preserves modes, so rsync -a alone would reproduce them and
    the manifest replay would look correct while doing nothing. Flattening first is what
    makes the assertion about apply_modes rather than about rsync.
    """
    for path in snapshot.rglob('*'):
        path.chmod(0o755 if path.is_dir() else 0o644)


def test_restore_repairs_modes_the_destination_could_not_store(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    target = tmp_path / 'target'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
    run_safekeep('--config', str(config_path), 'backup')

    snapshot = next(d for d in dest.iterdir() if d.is_dir())
    flatten_modes(snapshot)
    stored = snapshot / safekeep.snapshot_rel(source_tree / 'notes')
    assert stat.S_IMODE((stored / 'secret.txt').stat().st_mode) == 0o644

    result = run_safekeep('--config', str(config_path), 'restore', '--to', str(target), '--all')
    assert result.returncode == 0, result.stderr

    restored = target / safekeep.snapshot_rel(source_tree / 'notes')
    assert stat.S_IMODE((restored / 'secret.txt').stat().st_mode) == 0o600
    assert stat.S_IMODE((restored / 'run.sh').stat().st_mode) == 0o755
    assert stat.S_IMODE((restored / 'plain.md').stat().st_mode) == 0o644


def test_restore_dry_run_reports_recorded_modes_not_zero(tmp_path, source_tree):
    """secret.txt at 0600, run.sh at 0755, solo.conf at 0600 — the three deviations from the
    defaults the manifest records."""
    restore, _ = backup_and_restore(tmp_path, source_tree, '--all', '--dry-run')
    assert restore.returncode == 0, restore.stderr
    assert 'would reapply 3 recorded modes' in plain(restore.stdout)


def test_restore_by_tag_selects_only_the_groups_carrying_it(tmp_path, source_tree):
    """'secrets' is on solo.conf alone, so the other two groups must stay out of the target —
    a tag on every group cannot show that the filter discriminates."""
    restore, target = backup_and_restore(tmp_path, source_tree, '--tag', 'secrets')
    assert restore.returncode == 0, restore.stderr
    assert (target / safekeep.snapshot_rel(source_tree / 'solo.conf')).read_text() == 'solo\n'
    assert not (target / safekeep.snapshot_rel(source_tree / 'notes')).exists()
    assert not (target / safekeep.snapshot_rel(source_tree / 'linked.conf')).exists()


def test_restore_by_tag_takes_every_group_carrying_it(tmp_path, source_tree):
    restore, target = backup_and_restore(tmp_path, source_tree, '--tag', 'docs')
    assert restore.returncode == 0, restore.stderr
    assert (target / safekeep.snapshot_rel(source_tree / 'notes') / 'plain.md').exists()
    assert (target / safekeep.snapshot_rel(source_tree / 'solo.conf')).exists()
    assert (target / safekeep.snapshot_rel(source_tree / 'linked.conf')).exists()


def test_restore_by_group_matches_the_source_path(tmp_path, source_tree):
    """--group is a substring of the source, and it has to discriminate: select_groups was only
    ever asserted against a hand-built manifest, never against one a backup wrote."""
    restore, target = backup_and_restore(tmp_path, source_tree, '--group', 'solo.conf')
    assert restore.returncode == 0, restore.stderr
    assert (target / safekeep.snapshot_rel(source_tree / 'solo.conf')).exists()
    assert not (target / safekeep.snapshot_rel(source_tree / 'notes')).exists()


def test_restore_by_unknown_group_lists_the_sources_the_snapshot_has(tmp_path, source_tree):
    restore, target = backup_and_restore(tmp_path, source_tree, '--group', 'nowhere')
    assert restore.returncode == 1
    assert str(source_tree / 'solo.conf') in plain(restore.stderr)
    assert not target.exists()


def test_restore_by_unknown_tag_says_which_tags_the_snapshot_has(tmp_path, source_tree):
    """An explicit selection that matched nothing is a failed request, not a cancelled one — so
    it exits non-zero and names the tags the snapshot actually carries. 'nothing selected' alone
    left no way to tell a typo from a tag added to the config after the snapshot was taken."""
    restore, target = backup_and_restore(tmp_path, source_tree, '--tag', 'nope')
    assert restore.returncode == 1
    assert 'nothing selected' in restore.stderr
    assert 'tags in this snapshot: docs' in plain(restore.stderr)
    assert not target.exists()


def test_restore_dry_run_writes_nothing(tmp_path, source_tree):
    """The rehearsal: every group shape, into a target that does not exist yet. The exit code
    is half the assertion — an empty target is equally what a crash leaves behind, and a crash
    is exactly what a single-file group used to produce here (rsync cannot be given a file
    destination under a directory it is not allowed to create)."""
    restore, target = backup_and_restore(tmp_path, source_tree, '--all', '--dry-run')
    assert restore.returncode == 0, restore.stderr
    assert 'would restore 3 groups' in plain(restore.stdout)
    assert not target.exists()


def conflicting(tmp_path, source_tree, policy, existing_text='mine\n', existing_age=None):
    """Restore over a target that already holds `existing_text`, under one conflict policy.

    Both shapes conflict: plain.md inside the directory group, and solo.conf as a group that is
    itself a file. rsync is given a different destination form for each, so a policy verified on
    one is not verified on the other.

    `existing_age` offsets the pre-existing file's mtime from now, in seconds. A --update test
    has to state which side is newer rather than inherit whatever ordering fell out of how long
    the backup took — the file is written after the snapshot, so by default it is the newer one.
    """
    dest = tmp_path / 'dest'
    target = tmp_path / 'target'
    config_path = matrix_config(tmp_path, dest, source_tree)
    backup = run_safekeep('--config', str(config_path), 'backup')
    assert backup.returncode == 0, backup.stderr

    in_dir = target / safekeep.snapshot_rel(source_tree / 'notes') / 'plain.md'
    as_file = target / safekeep.snapshot_rel(source_tree / 'solo.conf')
    for path in (in_dir, as_file):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(existing_text)
        if existing_age is not None:
            when = time.time() + existing_age
            os.utime(path, (when, when))

    result = run_safekeep('--config', str(config_path), 'restore', '--to', str(target), '--all', '--on-conflict', policy)
    assert result.returncode == 0, result.stderr
    return in_dir, as_file


def test_restore_backs_up_conflicting_files_by_default(tmp_path, source_tree):
    in_dir, as_file = conflicting(tmp_path, source_tree, 'backup')
    assert in_dir.read_text() == 'plain\n'
    assert in_dir.with_name('plain.md.pre-restore').read_text() == 'mine\n'
    assert as_file.read_text() == 'solo\n'
    assert as_file.with_name('solo.conf.pre-restore').read_text() == 'mine\n'


def test_restore_skip_conflict_leaves_existing_alone(tmp_path, source_tree):
    in_dir, as_file = conflicting(tmp_path, source_tree, 'skip')
    assert in_dir.read_text() == 'mine\n'
    assert as_file.read_text() == 'mine\n'


def test_restore_overwrite_conflict_keeps_no_copy(tmp_path, source_tree):
    """overwrite is the policy with nothing to fall back on, so the absence of a .pre-restore
    file beside it is the assertion that matters."""
    in_dir, as_file = conflicting(tmp_path, source_tree, 'overwrite')
    assert in_dir.read_text() == 'plain\n'
    assert as_file.read_text() == 'solo\n'
    assert not in_dir.with_name('plain.md.pre-restore').exists()
    assert not as_file.with_name('solo.conf.pre-restore').exists()


def test_restore_newer_conflict_keeps_a_target_file_that_is_newer(tmp_path, source_tree):
    in_dir, as_file = conflicting(tmp_path, source_tree, 'newer', existing_age=60)
    assert in_dir.read_text() == 'mine\n'
    assert as_file.read_text() == 'mine\n'


def test_restore_newer_conflict_replaces_a_target_file_that_is_older(tmp_path, source_tree):
    """The other half: --update holds a file back only when it is genuinely the newer one, so
    the same policy over an older target has to write. Without this, 'newer' passes its test by
    never restoring anything."""
    in_dir, as_file = conflicting(tmp_path, source_tree, 'newer', existing_age=-3600)
    assert in_dir.read_text() == 'plain\n'
    assert as_file.read_text() == 'solo\n'


def test_restore_counts_and_sizes_each_group_as_it_reaches_it(tmp_path, source_tree):
    """A restore compares by checksum and then reapplies a mode per restored path, which on a
    real tree is minutes with nothing on screen. Each source names itself, its position, and the
    size the manifest recorded before rsync is called on it, so a wait belongs to a named group
    rather than to the tool as a whole."""
    restore, _ = backup_and_restore(tmp_path, source_tree, '--all')
    out = plain(restore.stdout)
    for position, name in enumerate(['notes', 'solo.conf', 'linked.conf'], start=1):
        assert re.search(rf'\[{position}/3\] \S*{re.escape(name)}\s+\d+ files?\s+\d+ B', out), f'{name} is not counted and sized'


def test_restore_writes_no_redraw_sequences_when_redirected(tmp_path, source_tree):
    """The live lines redraw with a carriage return, which collapses a captured log into one
    unreadable line — and a restore is exactly the thing run under tee. Off a terminal the
    per-group lines carry the whole report on their own."""
    restore, _ = backup_and_restore(tmp_path, source_tree, '--all')
    assert '\r' not in restore.stdout


def test_restore_skips_a_group_missing_from_the_snapshot(tmp_path, source_tree):
    """A manifest can name a group whose files are not there — a snapshot copied in part, or a
    manifest merged from a run whose files were later removed by hand."""
    dest = tmp_path / 'dest'
    target = tmp_path / 'target'
    config_path = matrix_config(tmp_path, dest, source_tree)
    run_safekeep('--config', str(config_path), 'backup')

    snapshot = next(d for d in dest.iterdir() if d.is_dir())
    (snapshot / safekeep.snapshot_rel(source_tree / 'solo.conf')).unlink()

    result = run_safekeep('--config', str(config_path), 'restore', '--to', str(target), '--all')
    assert result.returncode == 0, result.stderr
    assert 'not present in snapshot' in plain(result.stdout)
    assert 'restored 2 groups' in plain(result.stdout)
    assert (target / safekeep.snapshot_rel(source_tree / 'notes') / 'plain.md').exists()


def test_restore_reports_dereferenced_symlinks(tmp_path, source_tree):
    restore, target = backup_and_restore(tmp_path, source_tree, '--all')
    assert 'were symlinks when backed up' in restore.stdout
    assert (target / safekeep.snapshot_rel(source_tree / 'linked.conf')).is_file()


def test_restore_skip_symlinked_omits_only_the_symlinked_group(tmp_path, source_tree):
    restore, target = backup_and_restore(tmp_path, source_tree, '--all', '--skip-symlinked')
    assert restore.returncode == 0, restore.stderr
    assert 'was a symlink' in plain(restore.stdout)
    assert not (target / safekeep.snapshot_rel(source_tree / 'linked.conf')).exists()
    assert (target / safekeep.snapshot_rel(source_tree / 'solo.conf')).exists()
    assert (target / safekeep.snapshot_rel(source_tree / 'notes') / 'plain.md').exists()


def test_restore_refuses_a_snapshot_without_a_manifest(tmp_path):
    dest = tmp_path / 'dest'
    (dest / '2026-01-01').mkdir(parents=True)
    config_path = write_config(tmp_path, dest)
    result = run_safekeep('--config', str(config_path), 'restore', '--to', str(tmp_path / 't'), '--from', '2026-01-01', '--all')
    assert result.returncode == 1
    assert 'no manifest' in result.stderr
    assert 'rsync' in result.stderr


def test_remap_home_rewrites_only_the_home_prefix():
    assert safekeep.remap_home('/home/old/notes', '/home/old', '/home/new') == '/home/new/notes'
    assert safekeep.remap_home('/home/old', '/home/old', '/home/new') == '/home/new'
    assert safekeep.remap_home('/mnt/c/docs', '/home/old', '/home/new') == '/mnt/c/docs'
    assert safekeep.remap_home('/home/older/x', '/home/old', '/home/new') == '/home/older/x'


def test_restore_remaps_a_different_home(tmp_path, source_tree):
    """A snapshot taken under another user's home lands under this one's."""
    dest = tmp_path / 'dest'
    target = tmp_path / 'target'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
    run_safekeep('--config', str(config_path), 'backup')

    snapshot = next(d for d in dest.iterdir() if d.is_dir())
    manifest_path = snapshot / safekeep.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())

    fake_home = '/home/someone-else'
    moved = fake_home + '/notes'
    manifest['home'] = fake_home
    manifest['groups'][0]['source'] = moved
    manifest['modes'] = {safekeep.snapshot_rel(moved + '/secret.txt'): '0600'}
    manifest_path.write_text(json.dumps(manifest))

    stored = snapshot / safekeep.snapshot_rel(source_tree / 'notes')
    relocated = snapshot / safekeep.snapshot_rel(moved)
    relocated.parent.mkdir(parents=True, exist_ok=True)
    stored.rename(relocated)

    run_safekeep('--config', str(config_path), 'restore', '--to', str(target), '--all')

    landed = target / safekeep.snapshot_rel(str(Path.home()) + '/notes')
    assert (landed / 'plain.md').read_text() == 'plain\n'
    assert stat.S_IMODE((landed / 'secret.txt').stat().st_mode) == 0o600


def test_group_selection_matches_on_substring():
    manifest = {
        'groups': [
            {'kind': 'path', 'source': '/home/c/notes', 'tags': []},
            {'kind': 'path', 'source': '/mnt/c/docs', 'tags': ['windows']},
        ]
    }
    args = type('Args', (), {'all': False, 'group': ['notes'], 'tag': []})()
    assert [g['source'] for g in safekeep.select_groups(manifest, args)] == ['/home/c/notes']


def test_group_selection_returns_none_when_nothing_specified():
    args = type('Args', (), {'all': False, 'group': [], 'tag': []})()
    assert safekeep.select_groups({'groups': []}, args) is None


def test_repo_groups_sharing_a_subtree_are_restored_once(tmp_path):
    """The untracked and ignored groups come from the same repo, so the subtree copies once."""
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / '.gitignore').write_text('secrets.env\n')
    git_commit_all(repo, '.gitignore')
    (repo / 'wip.txt').write_text('wip\n')
    (repo / 'secrets.env').write_text('KEY=1\n')

    dest = tmp_path / 'dest'
    target = tmp_path / 'target'
    config_path = write_config(tmp_path, dest, git={'repos': [{'path': str(repo)}], 'back_up_ignored_files_matching': ['secrets.env']})
    run_safekeep('--config', str(config_path), 'backup')

    snapshot = next(d for d in dest.iterdir() if d.is_dir())
    manifest = json.loads((snapshot / safekeep.MANIFEST_NAME).read_text())
    assert {g['kind'] for g in manifest['groups']} == {'git_untracked', 'git_ignored'}

    result = run_safekeep('--config', str(config_path), 'restore', '--to', str(target), '--all')
    assert result.returncode == 0, result.stderr
    restored = target / safekeep.snapshot_rel(repo)
    assert (restored / 'wip.txt').read_text() == 'wip\n'
    assert (restored / 'secrets.env').read_text() == 'KEY=1\n'
    assert 'restored 1 group ' in plain(result.stdout)


def test_fzf_is_only_required_for_interactive_selection(tmp_path, source_tree):
    """Non-interactive restore must not depend on fzf being installed."""
    dest = tmp_path / 'dest'
    target = tmp_path / 'target'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
    run_safekeep('--config', str(config_path), 'backup')

    bin_dir = tmp_path / 'fzf-less-bin'
    bin_dir.mkdir()
    for tool in ('rsync', 'git'):
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)

    result = subprocess.run(
        [sys.executable, '-m', 'safekeep', '--config', str(config_path), 'restore', '--to', str(target), '--all'],
        capture_output=True,
        text=True,
        env=dict(os.environ, PATH=str(bin_dir)),
    )
    assert result.returncode == 0, result.stderr
    assert (target / safekeep.snapshot_rel(source_tree / 'notes') / 'plain.md').exists()


# --- version and self-update ------------------------------------------------------------


def test_version_flag_prints_the_running_version():
    result = run_safekeep('--version')
    assert result.returncode == 0, result.stderr
    assert safekeep.tool_version() in result.stdout


def test_version_is_read_from_the_installed_metadata():
    """Not a constant in the source. semantic-release writes pyproject, and a second
    copy in the module would be the one that goes stale."""
    assert safekeep.tool_version() != 'unknown', 'the test environment installs safekeep'
    assert 'safekeep' not in safekeep.tool_version()


def test_update_is_a_command_rather_than_a_usage_error():
    """Parsed here rather than run, because running it reaches the network. That the
    verb resolves at all is what a typo in the parser would break."""
    args = safekeep.build_parser().parse_args(['update'])
    assert args.command == 'update'
