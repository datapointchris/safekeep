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
import pty
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
    for selection in ('--all', '--source', '--tag'):
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


def test_every_command_in_the_tree_is_documented(tmp_path):
    """The fzf preview panes used to run two hidden preview-* commands. They are `snapshots
    show` now, so nothing in the tree is undocumented and the old names are gone."""
    result = run_safekeep('--help')
    assert 'preview-snapshot' not in result.stdout
    assert 'preview-source' not in result.stdout
    assert 'SUPPRESS' not in result.stdout
    assert 'safekeep snapshots show' in result.stdout
    assert run_safekeep('preview-snapshot', '2026-08-13').returncode == 2


@pytest.mark.parametrize('namespace', ['backup', 'snapshots', 'tags', 'config'])
def test_a_bare_namespace_shows_its_own_help(tmp_path, namespace):
    """A namespace names a resource without selecting a verb, so it prints the screen that
    completes the command line rather than guessing which verb was meant."""
    result = run_safekeep(namespace)
    assert result.returncode == 2
    assert f'safekeep {namespace}' in result.stdout
    assert result.stderr == ''


def test_a_bare_backup_writes_nothing(tmp_path, source_tree):
    """The whole point of the verb: `backup` used to copy every configured path the moment it
    was typed, so walking the tree one token at a time wrote to the destination."""
    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
    result = run_safekeep('--config', str(config_path), 'backup')
    assert result.returncode == 2
    assert 'safekeep backup run' in result.stdout
    assert not dest.exists()


def test_a_namespace_screen_names_its_verbs(tmp_path):
    for namespace, verbs in (('snapshots', ('list', 'show')), ('tags', ('list', 'show')), ('backup', ('run',))):
        result = run_safekeep(namespace, '--help')
        assert result.returncode == 0
        for verb in verbs:
            assert f'safekeep {namespace} {verb}' in result.stdout


def test_a_verb_missing_its_argument_says_which_one(tmp_path, source_tree):
    """Both positionals are optional in the parser so `--help` reaches its screen; the error
    that replaces argparse's names the argument and where to find its values."""
    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
    for verb, wanted in (('snapshots', 'snapshots list'), ('tags', 'tags list')):
        result = run_safekeep('--config', str(config_path), verb, 'show')
        assert result.returncode == 2
        assert wanted in result.stderr


def test_a_verbs_help_is_its_namespaces_screen(tmp_path):
    """One screen per namespace, so `backup --help` and `backup run --help` reach the same page
    rather than a drill-down you read to learn there was nothing on it."""
    assert run_safekeep('backup', 'run', '--help').stdout == run_safekeep('backup', '--help').stdout


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


def test_normalize_entries_expands_a_variable_in_the_path(monkeypatch):
    """A file whose location differs per machine is declared as a variable and set on each
    one, so the same config text backs up the right file everywhere. dotfiles generates a
    block carrying $REPOS_JSON for exactly this reason — it cannot resolve another
    machine's registry, and must not write its own answer into that machine's config."""
    monkeypatch.setenv('REPOS_JSON', '/declared/repos.json')
    entries = safekeep.normalize_entries([{'path': '$REPOS_JSON'}])
    assert entries[0] == (Path('/declared/repos.json'), [])


def test_normalize_entries_leaves_an_unset_variable_literal(monkeypatch):
    """Which makes the path not exist, reported as a missing path rather than passing
    silently. Substituting a default here would back up the wrong file and say nothing."""
    monkeypatch.delenv('REPOS_JSON', raising=False)
    entries = safekeep.normalize_entries([{'path': '$REPOS_JSON'}])
    assert entries[0] == (Path('$REPOS_JSON'), [])


def test_normalize_entries_expands_a_variable_before_the_tilde(monkeypatch):
    """Order matters: a variable holding a ~-relative path has to expand to a real one."""
    monkeypatch.setenv('REPOS_JSON', '~/declared/repos.json')
    entries = safekeep.normalize_entries([{'path': '$REPOS_JSON'}])
    assert entries[0] == (Path.home() / 'declared' / 'repos.json', [])


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


def test_survey_records_the_source_directory_itself(tmp_path):
    """~/.ssh and ~/.config/gnupg are 0700, and the source root's own mode used to be the one
    path the survey skipped — so a rebuild recreated them at the 0755 default and gpg refused a
    homedir anyone could read."""
    private = tmp_path / 'gnupg'
    private.mkdir()
    private.chmod(0o700)
    (private / 'pubring.kbx').write_text('keys\n')

    survey = safekeep.survey_tree(private, [], None)
    assert survey['modes'][safekeep.snapshot_rel(private)] == '0700'


def test_restore_recreates_a_private_directory_at_its_own_mode(tmp_path):
    """The round trip of the above, through a destination that cannot store modes at all."""
    dest = tmp_path / 'dest'
    target = tmp_path / 'target'
    private = tmp_path / 'gnupg'
    private.mkdir()
    private.chmod(0o700)
    (private / 'pubring.kbx').write_text('keys\n')

    config_path = write_config(tmp_path, dest, back_up_paths=paths(private))
    run_safekeep('--config', str(config_path), 'backup', 'run')
    flatten_modes(next(d for d in dest.iterdir() if d.is_dir()))

    result = run_safekeep('--config', str(config_path), 'restore', '--to', str(target), '--all')
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE((target / safekeep.snapshot_rel(private)).stat().st_mode) == 0o700


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
    result = run_safekeep('--config', str(config_path), 'backup', 'run')
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

    result = run_safekeep('--config', str(config_path), 'backup', 'run', '--tag', 'conf')
    assert result.returncode == 0, result.stderr

    snapshot = next(d for d in dest.iterdir() if d.is_dir())
    manifest = json.loads((snapshot / safekeep.MANIFEST_NAME).read_text())
    assert [group['source'] for group in manifest['groups']] == [str(source_tree / 'real.conf')]


def test_a_narrowed_backup_keeps_the_groups_it_did_not_cover(tmp_path, source_tree):
    """rsync never deletes, so the untouched files are still in the snapshot — dropping their
    groups would leave them on disk and unrestorable, since the manifest is the only record."""
    dest = tmp_path / 'dest'
    config_path = tagged(tmp_path, dest, (source_tree / 'notes', ['docs']), (source_tree / 'real.conf', ['conf']))
    run_safekeep('--config', str(config_path), 'backup', 'run')
    run_safekeep('--config', str(config_path), 'backup', 'run', '--tag', 'conf')

    snapshot = next(d for d in dest.iterdir() if d.is_dir())
    manifest = json.loads((snapshot / safekeep.MANIFEST_NAME).read_text())
    assert {group['source'] for group in manifest['groups']} == {str(source_tree / 'notes'), str(source_tree / 'real.conf')}
    assert manifest['modes'], 'the modes recorded for the untouched group survive the merge too'

    target = tmp_path / 'target'
    restore = run_safekeep('--config', str(config_path), 'restore', '--to', str(target), '--tag', 'docs')
    assert restore.returncode == 0, restore.stderr
    assert (target / safekeep.snapshot_rel(source_tree / 'notes') / 'plain.md').exists()


def labelled_snapshot(tmp_path, dest, source_tree, *runs):
    """Back up once per entry in `runs`, each a --label argument list, and read the manifest."""
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
    for extra in runs:
        result = run_safekeep('--config', str(config_path), 'backup', 'run', *extra)
        assert result.returncode == 0, result.stderr
    snapshot = next(d for d in dest.iterdir() if d.is_dir())
    return config_path, json.loads((snapshot / safekeep.MANIFEST_NAME).read_text())


def test_a_label_says_why_the_backup_was_taken(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    _, manifest = labelled_snapshot(tmp_path, dest, source_tree, ['--label', 'before moving wsl instance'])
    assert manifest['label'] == 'before moving wsl instance'


def test_a_backup_without_a_label_records_none(tmp_path, source_tree):
    """Absent rather than empty: nothing reads the key, so a snapshot that was never labelled
    should not claim a field it has no answer for."""
    dest = tmp_path / 'dest'
    _, manifest = labelled_snapshot(tmp_path, dest, source_tree, [])
    assert 'label' not in manifest


def test_a_later_run_that_day_keeps_the_label_already_there(tmp_path, source_tree):
    """One snapshot per date, so a routine run merges into the one taken before the risky
    thing. It must not erase the note that run wrote."""
    dest = tmp_path / 'dest'
    _, manifest = labelled_snapshot(tmp_path, dest, source_tree, ['--label', 'before the wsl move'], [])
    assert manifest['label'] == 'before the wsl move'


def test_a_later_run_that_day_can_replace_the_label(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    _, manifest = labelled_snapshot(tmp_path, dest, source_tree, ['--label', 'first'], ['--label', 'second'])
    assert manifest['label'] == 'second'


def test_an_empty_label_clears_the_one_already_there(tmp_path, source_tree):
    """The flag was typed, so it is a decision rather than an omission — and it is the only way
    to take a note back off a snapshot."""
    dest = tmp_path / 'dest'
    _, manifest = labelled_snapshot(tmp_path, dest, source_tree, ['--label', 'wrong'], ['--label', ''])
    assert manifest['label'] is None


def test_a_backup_reports_the_label_the_snapshot_ends_up_with(tmp_path, source_tree):
    """Read off the merged manifest, not off the flag, so a run that passed none still says
    which note today's snapshot is carrying."""
    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
    run_safekeep('--config', str(config_path), 'backup', 'run', '--label', 'before the wsl move')
    result = run_safekeep('--config', str(config_path), 'backup', 'run')
    assert 'before the wsl move' in plain(result.stdout)


def test_a_dry_run_records_no_label(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
    result = run_safekeep('--config', str(config_path), 'backup', 'run', '-n', '--label', 'never written')
    assert result.returncode == 0
    assert not any(dest.iterdir()), 'a dry run creates the destination base and no snapshot in it'


def test_backup_by_unknown_tag_is_a_usage_error(tmp_path, source_tree):
    """Backing up nothing reads exactly like backing up everything asked for, so a typo has to
    fail rather than succeed at covering nothing."""
    dest = tmp_path / 'dest'
    config_path = tagged(tmp_path, dest, (source_tree / 'notes', ['docs']))

    result = run_safekeep('--config', str(config_path), 'backup', 'run', '--tag', 'nope')
    assert result.returncode == 2
    assert 'docs' in result.stderr
    assert not dest.exists()


def test_backup_by_unknown_group_lists_the_paths_there_are(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = tagged(tmp_path, dest, (source_tree / 'notes', ['docs']))

    result = run_safekeep('--config', str(config_path), 'backup', 'run', '--source', 'zzz')
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
    result = run_safekeep('--config', str(config_path), 'backup', 'run')
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
    result = run_safekeep('--config', str(config_path), 'backup', 'run')
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

    result = run_safekeep('--config', str(config_path), 'backup', 'run')
    assert result.returncode == 0, result.stderr
    assert 'could not list untracked files' in plain(result.stdout)


def test_backup_names_every_file_it_skipped_for_size(tmp_path, source_tree):
    """A short backup is never silently short: the oversized file stays out of the snapshot and
    is named in the manifest, so a restore that comes up missing has a written reason."""
    dest = tmp_path / 'dest'
    big = source_tree / 'notes' / 'big.bin'
    big.write_bytes(b'0' * (2 * 1024 * 1024))
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'), skip_files_over_mb=1)

    result = run_safekeep('--config', str(config_path), 'backup', 'run')
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
        result = run_safekeep('--config', str(config_path), 'backup', 'run')
        assert result.returncode == 1
        assert 'not writable' in result.stderr
        assert str(dest) in plain(result.stderr)
    finally:
        dest.chmod(0o755)


def test_backup_records_config_warnings_in_manifest(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'), keep=5)
    run_safekeep('--config', str(config_path), 'backup', 'run')

    snapshot = next(d for d in dest.iterdir() if d.is_dir())
    manifest = json.loads((snapshot / safekeep.MANIFEST_NAME).read_text())
    assert any('keep' in w for w in manifest['config_warnings'])


def test_dry_run_writes_nothing(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
    run_safekeep('--config', str(config_path), 'backup', 'run', '--dry-run')
    assert not any(dest.iterdir())


def test_backup_does_not_prune_old_snapshots(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    dest.mkdir()
    for old in ('2020-01-01', '2020-01-02', '2020-01-03'):
        (dest / old).mkdir()
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
    run_safekeep('--config', str(config_path), 'backup', 'run')
    assert (dest / '2020-01-01').exists()
    assert (dest / '2020-01-03').exists()


def age_todays_snapshot(dest, to_date='2020-01-01'):
    """Rename the snapshot just written so the next run sees it as the previous one.

    do_backup names the directory from datetime.now(), so a second snapshot cannot be produced
    within a test any other way. Renaming exercises the real lookup rather than a stub, because
    previous_snapshot only ever reads directory names.
    """
    today = next(d for d in dest.iterdir() if d.is_dir())
    aged = dest / to_date
    today.rename(aged)
    return aged


def snapshot_copy_of(snapshot_dir, source_file):
    return snapshot_dir / safekeep.snapshot_rel(source_file)


def test_an_unchanged_file_is_hard_linked_into_the_next_snapshot(tmp_path, source_tree):
    """The dedup that made backup-incremental a separate tool, inside the tool with the manifest."""
    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
    plain = source_tree / 'notes' / 'plain.md'

    run_safekeep('--config', str(config_path), 'backup', 'run')
    previous = age_todays_snapshot(dest)
    run_safekeep('--config', str(config_path), 'backup', 'run')
    current = next(d for d in dest.iterdir() if d.is_dir() and d != previous)

    assert snapshot_copy_of(current, plain).stat().st_ino == snapshot_copy_of(previous, plain).stat().st_ino


def test_a_changed_file_is_copied_rather_than_linked(tmp_path, source_tree):
    """The hazard the linking creates: a shared inode must never carry a new version."""
    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
    plain = source_tree / 'notes' / 'plain.md'

    run_safekeep('--config', str(config_path), 'backup', 'run')
    previous = age_todays_snapshot(dest)
    plain.write_text('edited\n')
    run_safekeep('--config', str(config_path), 'backup', 'run')
    current = next(d for d in dest.iterdir() if d.is_dir() and d != previous)

    assert snapshot_copy_of(current, plain).read_text() == 'edited\n'
    assert snapshot_copy_of(previous, plain).read_text() == 'plain\n'
    assert snapshot_copy_of(current, plain).stat().st_ino != snapshot_copy_of(previous, plain).stat().st_ino


def test_the_manifest_names_the_snapshot_it_shares_inodes_with(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))

    run_safekeep('--config', str(config_path), 'backup', 'run')
    previous = age_todays_snapshot(dest)
    assert json.loads((previous / safekeep.MANIFEST_NAME).read_text())['linked_from'] is None

    run_safekeep('--config', str(config_path), 'backup', 'run')
    current = next(d for d in dest.iterdir() if d.is_dir() and d != previous)
    assert json.loads((current / safekeep.MANIFEST_NAME).read_text())['linked_from'] == '2020-01-01'


def test_linked_from_reads_inodes_rather_than_the_rsync_flag(tmp_path, source_tree):
    """Passing --link-dest is not evidence it happened: openrsync lacks the option, and a
    destination can refuse link() and leave rsync copying. Neither is reported. The field named
    a snapshot it might share nothing with, which is the one thing it existed to answer."""
    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
    run_safekeep('--config', str(config_path), 'backup', 'run')
    previous = age_todays_snapshot(dest)

    # rsync links on a content match, so the link-dest copies are made to differ from the source.
    # --link-dest is still passed and still finds nothing to link, which is the shape of a
    # destination that refuses link(). The old code recorded the name regardless.
    for stored in previous.rglob('*'):
        if stored.is_file() and stored.name != safekeep.MANIFEST_NAME:
            stored.write_text('different\n')

    run_safekeep('--config', str(config_path), 'backup', 'run')
    current = next(d for d in dest.iterdir() if d.is_dir() and d != previous)
    assert json.loads((current / safekeep.MANIFEST_NAME).read_text())['linked_from'] is None


def test_a_snapshot_says_whether_it_shares_storage(tmp_path, source_tree):
    """The field only answers the question if it is readable without opening the JSON."""
    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
    run_safekeep('--config', str(config_path), 'backup', 'run')
    previous = age_todays_snapshot(dest)
    assert 'full copy' in run_safekeep('--config', str(config_path), 'snapshots', 'show', previous.name).stdout

    run_safekeep('--config', str(config_path), 'backup', 'run')
    current = next(d for d in dest.iterdir() if d.is_dir() and d != previous)
    shown = plain(run_safekeep('--config', str(config_path), 'snapshots', 'show', current.name).stdout)
    assert f'shares inodes with {previous.name}' in shown


def test_a_second_run_the_same_day_does_not_link_against_itself(tmp_path, source_tree):
    """Linking a snapshot in progress against itself would pin the version it is replacing."""
    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
    plain = source_tree / 'notes' / 'plain.md'

    run_safekeep('--config', str(config_path), 'backup', 'run')
    today = next(d for d in dest.iterdir() if d.is_dir())
    plain.write_text('same day edit\n')
    run_safekeep('--config', str(config_path), 'backup', 'run')

    assert json.loads((today / safekeep.MANIFEST_NAME).read_text())['linked_from'] is None
    assert snapshot_copy_of(today, plain).read_text() == 'same day edit\n'


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
    result = run_safekeep('--config', str(config_path), 'backup', 'run')
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
    result = run_safekeep('--config', str(config_path), 'snapshots', 'list')
    assert 'no manifest' in result.stdout


def test_both_snapshot_views_show_the_label(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path, _ = labelled_snapshot(tmp_path, dest, source_tree, ['--label', 'before moving wsl instance'])
    date = next(d for d in dest.iterdir() if d.is_dir()).name

    listed = plain(run_safekeep('--config', str(config_path), 'snapshots', 'list').stdout)
    assert 'before moving wsl instance' in listed
    assert date in listed.splitlines()[-1], 'the label goes after the columns, never onto its own row'

    shown = plain(run_safekeep('--config', str(config_path), 'snapshots', 'show', date).stdout)
    assert 'before moving wsl instance' in shown


def test_a_snapshot_without_a_label_renders_clean(tmp_path, source_tree):
    """The degradation case, and the one every snapshot taken before this existed lands in:
    no trailing separator, no empty column, nothing claiming a note that was never written."""
    dest = tmp_path / 'dest'
    config_path, manifest = labelled_snapshot(tmp_path, dest, source_tree, [])
    assert 'label' not in manifest
    date = next(d for d in dest.iterdir() if d.is_dir()).name

    row = plain(run_safekeep('--config', str(config_path), 'snapshots', 'list').stdout).splitlines()[-1]
    assert row == row.rstrip()
    assert row.endswith(os.uname().nodename)
    assert 'label' not in plain(run_safekeep('--config', str(config_path), 'snapshots', 'show', date).stdout)


LONG_LABEL = 'before moving the wsl instance to the new machine, ' + 'x' * 120


def test_a_long_label_survives_a_redirect_whole(tmp_path, source_tree):
    """The row is clipped to fit a terminal, and a redirected run has no width to fit — clip
    falls back to 80 columns rather than declining, so an unguarded call would truncate a
    captured log to a width nothing asked for."""
    dest = tmp_path / 'dest'
    config_path, _ = labelled_snapshot(tmp_path, dest, source_tree, ['--label', LONG_LABEL])
    listed = plain(run_safekeep('--config', str(config_path), 'snapshots', 'list').stdout)
    assert LONG_LABEL in listed
    assert '…' not in listed


def test_a_long_label_is_clipped_on_a_terminal(tmp_path, source_tree):
    """The other half of the gate above: on a terminal there is a width to fit, and a row that
    wraps is two rows — which is what stops a column of dates being scannable."""
    dest = tmp_path / 'dest'
    config_path, _ = labelled_snapshot(tmp_path, dest, source_tree, ['--label', LONG_LABEL])

    primary, secondary = pty.openpty()
    command = [sys.executable, '-m', 'safekeep', '--config', str(config_path), 'snapshots', 'list']
    process = subprocess.Popen(command, stdin=secondary, stdout=secondary, stderr=secondary, close_fds=True)
    os.close(secondary)
    output = b''
    try:
        while chunk := os.read(primary, 4096):
            output += chunk
    except OSError:
        pass
    process.wait()
    os.close(primary)

    row = next(line for line in plain(output.decode()).splitlines() if '2026' in line or 'x' in line)
    assert '…' in row
    assert LONG_LABEL not in row


def test_a_restore_names_the_label_of_the_snapshot_it_reads(tmp_path, source_tree):
    """A date says when a snapshot was taken and nothing about why, which is the question being
    answered when an older one is picked on purpose."""
    dest = tmp_path / 'dest'
    config_path, _ = labelled_snapshot(tmp_path, dest, source_tree, ['--label', 'before moving wsl instance'])
    result = run_safekeep('--config', str(config_path), 'restore', '--to', str(tmp_path / 'target'), '--all')
    assert result.returncode == 0, result.stderr
    assert 'before moving wsl instance' in plain(result.stdout)


def test_a_label_reaching_the_picker_holds_no_tab(tmp_path):
    """Picker rows are split on tabs, so a label carrying one would shift every field after it
    and a selection would be read out of the wrong column."""
    assert safekeep.fzf_cell('before\tthe\nwsl  move') == 'before the wsl move'
    assert safekeep.fzf_cell(None) == ''
    assert safekeep.fzf_cell('') == ''


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
    run_safekeep('--config', str(config_path), 'backup', 'run')

    result = run_safekeep('--config', str(config_path), 'tags', 'list')
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
    run_safekeep('--config', str(config_path), 'backup', 'run')

    tagged(tmp_path, dest, (source_tree / 'notes', ['docs']), (source_tree / 'real.conf', ['wsl']))
    out = plain(run_safekeep('--config', str(config_path), 'tags', 'list').stdout)
    assert 'wsl' in out
    assert 'not in this snapshot' in out


def test_tags_keeps_listing_a_tag_the_config_dropped(tmp_path, source_tree):
    """--tag selects on the snapshot, so a tag removed from the config is still the only name
    the snapshots taken before the removal answer to."""
    dest = tmp_path / 'dest'
    config_path = tagged(tmp_path, dest, (source_tree / 'notes', ['docs']))
    run_safekeep('--config', str(config_path), 'backup', 'run')

    tagged(tmp_path, dest, (source_tree / 'notes', ['rebuild']))
    assert 'docs' in plain(run_safekeep('--config', str(config_path), 'tags', 'list').stdout)
    detail = plain(run_safekeep('--config', str(config_path), 'tags', 'show', 'docs').stdout)
    assert 'tagged in the snapshot only' in detail


def test_tags_names_the_restore_that_would_bring_one_back(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = tagged(tmp_path, dest, (source_tree / 'notes', ['docs']))
    run_safekeep('--config', str(config_path), 'backup', 'run')

    out = plain(run_safekeep('--config', str(config_path), 'tags', 'show', 'docs').stdout)
    assert '--tag docs' in out
    assert str(source_tree / 'notes') in out


def test_tags_from_sizes_against_the_snapshot_named(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = tagged(tmp_path, dest, (source_tree / 'notes', ['docs']))
    run_safekeep('--config', str(config_path), 'backup', 'run')
    date = next(d.name for d, _ in safekeep.list_snapshots(dest))

    out = plain(run_safekeep('--config', str(config_path), 'tags', 'show', 'docs', '--from', date).stdout)
    assert f'--from {date}' in out, 'the restore it prints has to reach the snapshot it just sized'
    assert run_safekeep('--config', str(config_path), 'tags', 'list', '--from', '2020-01-01').returncode == 1


def test_unknown_tag_lists_the_ones_that_exist(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = tagged(tmp_path, dest, (source_tree / 'notes', ['docs']))
    result = run_safekeep('--config', str(config_path), 'tags', 'show', 'nope')
    assert result.returncode == 2  # usage error: a name that was never valid
    assert 'docs' in result.stderr


def test_tags_counts_the_sources_no_tag_reaches(tmp_path, source_tree):
    """An untagged source is restorable only with --all or --source, which is worth knowing
    before a rebuild rather than during one."""
    dest = tmp_path / 'dest'
    config_path = tagged(tmp_path, dest, (source_tree / 'notes', ['docs']), (source_tree / 'real.conf', []))
    out = plain(run_safekeep('--config', str(config_path), 'tags', 'list').stdout)
    assert 'untagged: 1 source' in out


def test_tags_works_before_the_first_backup(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = tagged(tmp_path, dest, (source_tree / 'notes', ['docs']))
    result = run_safekeep('--config', str(config_path), 'tags', 'list')
    assert result.returncode == 0
    assert 'no snapshots' in plain(result.stdout)


def test_a_bare_string_tag_is_fatal(tmp_path, source_tree):
    """tags = "wsl" is a list of characters to Python, and the only symptom would be a restore
    selecting nothing from a snapshot whose config plainly carries the tag."""
    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, back_up_paths=[{'path': str(source_tree / 'notes'), 'tags': 'wsl'}])
    result = run_safekeep('--config', str(config_path), 'tags', 'list')
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
    backup = run_safekeep('--config', str(config_path), 'backup', 'run')
    assert backup.returncode == 0, backup.stderr
    restore = run_safekeep('--config', str(config_path), 'restore', '--to', str(target), *restore_args)
    return restore, target


def test_restore_requires_to(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
    run_safekeep('--config', str(config_path), 'backup', 'run')
    result = run_safekeep('--config', str(config_path), 'restore', '--all')
    assert result.returncode == 2  # usage error, per cli-design.md
    assert '--to' in result.stderr


def test_restore_without_selection_is_an_error_when_not_a_tty(tmp_path, source_tree):
    restore, _ = backup_and_restore(tmp_path, source_tree)
    assert restore.returncode == 1
    assert 'nothing selected' in restore.stderr


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
    run_safekeep('--config', str(config_path), 'backup', 'run')

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


def test_restore_dry_run_reports_the_modes_it_would_set_not_zero(tmp_path, source_tree):
    """Nothing is written, so a count taken from the target would be zero and read as a restore
    that does not repair modes at all. secret.txt at 0600, run.sh at 0755 and solo.conf at 0600
    are the three deviations the manifest records — the rest take the defaults."""
    restore, _ = backup_and_restore(tmp_path, source_tree, '--all', '--dry-run')
    assert restore.returncode == 0, restore.stderr
    assert re.search(r'would set modes on \d+ paths \(3 recorded deviations\)', plain(restore.stdout))


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
    """--source is a substring of the source path, and it has to discriminate: select_groups was
    ever asserted against a hand-built manifest, never against one a backup wrote."""
    restore, target = backup_and_restore(tmp_path, source_tree, '--source', 'solo.conf')
    assert restore.returncode == 0, restore.stderr
    assert (target / safekeep.snapshot_rel(source_tree / 'solo.conf')).exists()
    assert not (target / safekeep.snapshot_rel(source_tree / 'notes')).exists()


def test_restore_by_unknown_group_lists_the_sources_the_snapshot_has(tmp_path, source_tree):
    restore, target = backup_and_restore(tmp_path, source_tree, '--source', 'nowhere')
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
    assert 'would restore 3 sources' in plain(restore.stdout)
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
    backup = run_safekeep('--config', str(config_path), 'backup', 'run')
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


def test_restore_counts_and_sizes_each_source_in_path_order(tmp_path, source_tree):
    """A restore compares by checksum and then sets a mode per restored path, which on a real
    tree is minutes with nothing on screen. Each source names itself, its position, and the size
    the manifest recorded before rsync is called on it, so a wait belongs to a named source
    rather than to the tool as a whole.

    Path order, not config order: the manifest's order is only meaningful to whoever wrote the
    config, and these are the same three entries the config lists in another order entirely."""
    restore, _ = backup_and_restore(tmp_path, source_tree, '--all')
    out = plain(restore.stdout)
    for position, name in enumerate(['linked.conf', 'notes', 'solo.conf'], start=1):
        assert re.search(rf'\[{position}/3\] \S*{re.escape(name)}\s+\d+ files?\s+\d+ B', out), f'{name} is out of order or unsized'


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
    run_safekeep('--config', str(config_path), 'backup', 'run')

    snapshot = next(d for d in dest.iterdir() if d.is_dir())
    (snapshot / safekeep.snapshot_rel(source_tree / 'solo.conf')).unlink()

    result = run_safekeep('--config', str(config_path), 'restore', '--to', str(target), '--all')
    assert result.returncode == 0, result.stderr
    assert 'not present in snapshot' in plain(result.stdout)
    assert 'restored 2 sources' in plain(result.stdout)
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
    run_safekeep('--config', str(config_path), 'backup', 'run')

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
    args = type('Args', (), {'all': False, 'source': ['notes'], 'tag': []})()
    assert [g['source'] for g in safekeep.select_groups(manifest, args)] == ['/home/c/notes']


def test_group_selection_returns_none_when_nothing_specified():
    args = type('Args', (), {'all': False, 'source': [], 'tag': []})()
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
    run_safekeep('--config', str(config_path), 'backup', 'run')

    snapshot = next(d for d in dest.iterdir() if d.is_dir())
    manifest = json.loads((snapshot / safekeep.MANIFEST_NAME).read_text())
    assert {g['kind'] for g in manifest['groups']} == {'git_untracked', 'git_ignored'}

    result = run_safekeep('--config', str(config_path), 'restore', '--to', str(target), '--all')
    assert result.returncode == 0, result.stderr
    restored = target / safekeep.snapshot_rel(repo)
    assert (restored / 'wip.txt').read_text() == 'wip\n'
    assert (restored / 'secrets.env').read_text() == 'KEY=1\n'
    assert 'restored 1 source ' in plain(result.stdout)


# --- what a restore says it is doing --------------------------------------------------


def repo_with_untracked_and_ignored(tmp_path):
    """A repo carrying one tracked file, one untracked, and one gitignored.

    tracked.sh is the file no snapshot holds and the one a restore must not touch — it is what
    a git clone puts back, and it is executable, so anything applying a default mode across the
    working tree shows up here as a lost +x bit.
    """
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / '.gitignore').write_text('secrets.env\n')
    (repo / 'tracked.sh').write_text('#!/bin/sh\n')
    (repo / 'tracked.sh').chmod(0o755)
    git_commit_all(repo, '.gitignore', 'tracked.sh')
    (repo / 'wip.txt').write_text('wip\n')
    (repo / 'secrets.env').write_text('KEY=1\n')
    return repo


def repo_config(tmp_path, dest, repo):
    return write_config(tmp_path, dest, git={'repos': [{'path': str(repo)}], 'back_up_ignored_files_matching': ['secrets.env']})


def test_restore_names_each_file_and_says_which_are_gitignored(tmp_path):
    """'restoring 2 files' says nothing about which two. The kind matters most for a repo, where
    an untracked file and a gitignored one land in the same subtree and are not the same thing
    to whoever is looking at the output."""
    dest = tmp_path / 'dest'
    repo = repo_with_untracked_and_ignored(tmp_path)
    config_path = repo_config(tmp_path, dest, repo)
    run_safekeep('--config', str(config_path), 'backup', 'run')

    result = run_safekeep('--config', str(config_path), 'restore', '--to', str(tmp_path / 'target'), '--all')
    assert result.returncode == 0, result.stderr
    out = plain(result.stdout)
    assert re.search(r'\+ wip\.txt\s+untracked', out), out
    assert re.search(r'\+ secrets\.env\s+ignored', out), out
    assert 'untracked + ignored' in out, 'the source line says what the repo contributes, not just its path'


def test_restore_leaves_the_modes_of_files_no_snapshot_holds(tmp_path):
    """The mode pass used to walk the target, which for a repo is the whole working tree — every
    tracked file got the default 0644 because the manifest had no mode for a file it never saw.
    Restoring two untracked files reported eleven thousand paths and stripped +x on the way."""
    dest = tmp_path / 'dest'
    repo = repo_with_untracked_and_ignored(tmp_path)
    config_path = repo_config(tmp_path, dest, repo)
    run_safekeep('--config', str(config_path), 'backup', 'run')

    target = tmp_path / 'target'
    standing = target / safekeep.snapshot_rel(repo)
    standing.mkdir(parents=True)
    (standing / 'tracked.sh').write_text('#!/bin/sh\n')
    (standing / 'tracked.sh').chmod(0o755)

    result = run_safekeep('--config', str(config_path), 'restore', '--to', str(target), '--all')
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE((standing / 'tracked.sh').stat().st_mode) == 0o755, 'a file the snapshot never held was chmodded'
    assert re.search(r'set modes on [1-9]\d? paths?', plain(result.stdout)), 'the count is of restored paths, not of the target tree'


def test_restore_says_which_files_it_replaced_and_kept_a_copy_of(tmp_path, source_tree):
    """The default policy renames what it replaces, and a .pre-restore file nobody was told about
    is indistinguishable from litter the next time the directory is read."""
    in_dir, _ = conflicting(tmp_path, source_tree, 'backup')
    assert in_dir.with_name('plain.md.pre-restore').exists()


def test_restore_reports_the_files_it_left_alone_as_unchanged(tmp_path, source_tree):
    """A second restore over an identical target transfers nothing, and silence there reads as a
    restore that failed to find its files rather than one with nothing to do."""
    dest = tmp_path / 'dest'
    target = tmp_path / 'target'
    config_path = matrix_config(tmp_path, dest, source_tree)
    run_safekeep('--config', str(config_path), 'backup', 'run')
    run_safekeep('--config', str(config_path), 'restore', '--to', str(target), '--all')

    again = run_safekeep('--config', str(config_path), 'restore', '--to', str(target), '--all')
    assert again.returncode == 0, again.stderr
    assert 'unchanged' in plain(again.stdout)
    assert '+ plain.md' not in plain(again.stdout), 'nothing was written, so nothing may claim to have been'


def test_restore_dry_run_names_the_files_it_would_write(tmp_path, source_tree):
    """A rehearsal into a directory that does not exist yet cannot ask rsync anything — the
    parent cannot be created without writing. The file list is what makes it a rehearsal."""
    restore, target = backup_and_restore(tmp_path, source_tree, '--all', '--dry-run')
    assert restore.returncode == 0, restore.stderr
    assert '+ plain.md' in plain(restore.stdout)
    assert not target.exists()


def test_ask_needs_something_to_ask(tmp_path, source_tree):
    """--on-conflict ask off a terminal would block on the first conflict with no way to answer,
    which on a restore is a hang holding an unfinished tree."""
    restore, _ = backup_and_restore(tmp_path, source_tree, '--all', '--on-conflict', 'ask')
    assert restore.returncode == 2
    assert 'this run cannot ask' in plain(restore.stderr)


def test_no_input_refuses_to_ask_even_on_a_terminal(tmp_path, source_tree):
    """The point of --no-input: rehearse the non-interactive path from a terminal, without
    having to fake a pipe to find out how the run would behave under cron."""
    dest = tmp_path / 'dest'
    target = tmp_path / 'target'
    config_path = matrix_config(tmp_path, dest, source_tree)
    assert run_safekeep('--config', str(config_path), 'backup', 'run').returncode == 0

    primary, secondary = pty.openpty()
    command = [
        sys.executable,
        '-m',
        'safekeep',
        '--config',
        str(config_path),
        '--no-input',
        'restore',
        '--to',
        str(target),
        '--all',
        '--on-conflict',
        'ask',
    ]
    process = subprocess.Popen(command, stdin=secondary, stdout=secondary, stderr=secondary, close_fds=True)
    os.close(secondary)
    output = b''
    try:
        while chunk := os.read(primary, 4096):
            output += chunk
    except OSError:
        pass
    assert process.wait(timeout=60) == 2
    os.close(primary)
    assert 'this run cannot ask' in plain(output.decode(errors='replace'))


def answering(tmp_path, config_path, target, answers, *extra):
    """Run a restore attached to a pty, feeding it `answers`, and return what it printed.

    A pty rather than a pipe because --on-conflict ask is gated on stdin being a terminal, and
    the gate is half of what is being tested.
    """
    primary, secondary = pty.openpty()
    command = [sys.executable, '-m', 'safekeep', '--config', str(config_path), 'restore', '--to', str(target), *extra]
    process = subprocess.Popen(command, stdin=secondary, stdout=secondary, stderr=secondary, close_fds=True)
    os.close(secondary)
    os.write(primary, answers.encode())
    output = b''
    try:
        while chunk := os.read(primary, 4096):
            output += chunk
    except OSError:
        pass
    assert process.wait(timeout=60) == 0
    os.close(primary)
    return plain(output.decode(errors='replace'))


def test_ask_restores_the_files_answered_yes_and_keeps_the_rest(tmp_path, source_tree):
    """The alternative to a .pre-restore copy: decide per file, up front, and keep no copies —
    the decision was made, so there is nothing to fall back to."""
    dest = tmp_path / 'dest'
    target = tmp_path / 'target'
    config_path = matrix_config(tmp_path, dest, source_tree)
    run_safekeep('--config', str(config_path), 'backup', 'run')

    notes = target / safekeep.snapshot_rel(source_tree / 'notes')
    notes.mkdir(parents=True)
    for name in ('plain.md', 'run.sh', 'secret.txt'):
        (notes / name).write_text('mine\n')

    out = answering(tmp_path, config_path, target, 'y\nn\nn\n', '--source', 'notes', '--on-conflict', 'ask')
    assert (notes / 'plain.md').read_text() == 'plain\n', 'the file answered yes was not restored'
    assert (notes / 'run.sh').read_text() == 'mine\n', 'a file answered no was overwritten anyway'
    assert (notes / 'secret.txt').read_text() == 'mine\n'
    assert not list(notes.glob('*.pre-restore')), 'ask was answered, so there is nothing to keep a copy for'
    assert '1 replaced · 2 kept' in out, out


def test_ask_can_be_answered_once_for_everything_remaining(tmp_path, source_tree):
    """Three prompts is fine and three hundred is not, so 'all' has to end the asking."""
    dest = tmp_path / 'dest'
    target = tmp_path / 'target'
    config_path = matrix_config(tmp_path, dest, source_tree)
    run_safekeep('--config', str(config_path), 'backup', 'run')

    notes = target / safekeep.snapshot_rel(source_tree / 'notes')
    notes.mkdir(parents=True)
    for name in ('plain.md', 'run.sh', 'secret.txt'):
        (notes / name).write_text('mine\n')

    answering(tmp_path, config_path, target, 'a\n', '--source', 'notes', '--on-conflict', 'ask')
    assert (notes / 'plain.md').read_text() == 'plain\n'
    assert (notes / 'run.sh').read_text() == '#!/bin/sh\necho hi\n'
    assert (notes / 'secret.txt').read_text() == 'secret\n'


def test_ask_quits_without_touching_what_it_had_not_reached(tmp_path, source_tree):
    dest = tmp_path / 'dest'
    target = tmp_path / 'target'
    config_path = matrix_config(tmp_path, dest, source_tree)
    run_safekeep('--config', str(config_path), 'backup', 'run')

    notes = target / safekeep.snapshot_rel(source_tree / 'notes')
    notes.mkdir(parents=True)
    for name in ('plain.md', 'run.sh', 'secret.txt'):
        (notes / name).write_text('mine\n')

    out = answering(tmp_path, config_path, target, 'q\n', '--all', '--on-conflict', 'ask')
    assert 'stopped here' in out
    assert (notes / 'plain.md').read_text() == 'mine\n'
    assert not (target / safekeep.snapshot_rel(source_tree / 'solo.conf')).exists()


def test_a_source_records_the_files_of_its_git_groups(tmp_path):
    """The manifest carries the file lists so a restore can label each file untracked or ignored
    without the repo being present — which on a rebuilt machine it is not."""
    dest = tmp_path / 'dest'
    repo = repo_with_untracked_and_ignored(tmp_path)
    config_path = repo_config(tmp_path, dest, repo)
    run_safekeep('--config', str(config_path), 'backup', 'run')

    snapshot = next(d for d in dest.iterdir() if d.is_dir())
    manifest = json.loads((snapshot / safekeep.MANIFEST_NAME).read_text())
    by_kind = {group['kind']: group for group in manifest['groups']}
    assert by_kind['git_untracked']['paths'] == [safekeep.snapshot_rel(repo / 'wip.txt')]
    assert by_kind['git_ignored']['paths'] == [safekeep.snapshot_rel(repo / 'secrets.env')]
    assert safekeep.file_kinds(manifest)[safekeep.snapshot_rel(repo / 'secrets.env')] == 'ignored'


def test_fzf_is_only_required_for_interactive_selection(tmp_path, source_tree):
    """Non-interactive restore must not depend on fzf being installed."""
    dest = tmp_path / 'dest'
    target = tmp_path / 'target'
    config_path = write_config(tmp_path, dest, back_up_paths=paths(source_tree / 'notes'))
    run_safekeep('--config', str(config_path), 'backup', 'run')

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
