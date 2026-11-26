"""Index integrity cases adapted from harness-core's manifest gate suite."""
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from threading import Thread

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = 'scripts/check-job-host-manifest.py'
MEMBER = 'launchd-kit/member.py'
CLEAN = 'value = 1\n'


def git(repo, *args):
    return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True).stdout


def blob(data):
    raw = data.encode()
    return hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


def build(tmp_path):
    repo = tmp_path / 'repo'
    (repo / 'scripts').mkdir(parents=True)
    shutil.copyfile(ROOT / SCRIPT, repo / SCRIPT)
    (repo / 'launchd-kit').mkdir()
    (repo / MEMBER).write_text(CLEAN)
    (repo / '.job-host-manifest').write_text(MEMBER + '\n')
    (repo / 'scripts/job-host-blobs.tsv').write_text(
        '# Source commit: ' + 'a' * 40 + '\n# path TAB immutable source git blob\n'
        + MEMBER + '\t' + blob(CLEAN) + '\n')
    git(repo, 'init', '-q')
    git(repo, 'add', '-A')
    return repo


def run(repo, cwd=None):
    return subprocess.run([sys.executable, '-I', str(repo / SCRIPT)], cwd=cwd or repo,
                          capture_output=True, text=True)


def test_covered_tree_passes_from_another_directory(tmp_path):
    repo = build(tmp_path)
    result = run(repo, tmp_path)
    assert result.returncode == 0, result.stdout
    assert 'all vendored paths, modes and source blobs match' in result.stdout


@pytest.mark.parametrize('change,reason', [
    ('missing', 'listed but not tracked'),
    ('mode', 'listed at mode 100755'),
    ('symlink', 'listed at mode 120000'),
    ('changed', 'changed from recorded source blob'),
    ('extra', 'vendored file is extra'),
    ('pin-set', 'manifest/pin member set differs'),
])
def test_index_integrity_refuses(tmp_path, change, reason):
    repo = build(tmp_path)
    if change == 'missing':
        git(repo, 'update-index', '--force-remove', MEMBER)
    elif change == 'mode':
        git(repo, 'update-index', '--chmod=+x', MEMBER)
    elif change == 'symlink':
        (repo / MEMBER).unlink()
        (repo / MEMBER).symlink_to('elsewhere')
        git(repo, 'add', MEMBER)
    elif change == 'changed':
        (repo / MEMBER).write_text('value = 2\n')
        git(repo, 'add', MEMBER)
        # A disk cleanup is not evidence of the staged bytes.
        (repo / MEMBER).write_text(CLEAN)
    elif change == 'extra':
        (repo / 'launchd-kit/extra.py').write_text('extra\n')
        git(repo, 'add', '-A')
    else:
        pin = repo / 'scripts/job-host-blobs.tsv'
        pin.write_text(pin.read_text() + 'launchd-kit/z.py\t' + blob(CLEAN) + '\n')
        git(repo, 'add', 'scripts/job-host-blobs.tsv')
    result = run(repo)
    assert result.returncode == 1, f'index integrity accepted {change}: {result.stdout}'
    assert reason in result.stdout, result.stdout


@pytest.mark.parametrize('text,reason', [
    ('', 'requires nonempty LF-terminated text'),
    (MEMBER, 'requires nonempty LF-terminated text'),
    (MEMBER + '\r\n', 'requires nonempty LF-terminated text'),
    (MEMBER + '\n\n', 'outside vendored roots'),
    ('Tree launchd-kit/\n' + MEMBER + '\n', 'outside vendored roots'),
    ('tree  launchd-kit/\n' + MEMBER + '\n', 'invalid tree line'),
    ('- ' + MEMBER + '\n', 'outside vendored roots'),
    ('```\n' + MEMBER + '\n```\n', 'outside vendored roots'),
    ('tree launchd-kit\n' + MEMBER + '\n', 'invalid tree line'),
    (MEMBER + '\ntree launchd-kit/\n', 'invalid tree line'),
    ('tree launchd-kit/\ntree launchd-kit/\n' + MEMBER + '\n', 'overlapping tree'),
    ('tree job-host/\n' + MEMBER + '\n', 'empty tree or member set'),
    (MEMBER + '\n' + MEMBER + '\n', 'paths not sorted and unique'),
    ('launchd-kit/z.py\n' + MEMBER + '\n', 'paths not sorted and unique'),
    ('launchd-kit/Member.py\n' + MEMBER + '\n', 'paths not sorted and unique'),
    ('launchd-kit/../member.py\n', 'invalid component'),
    ('launchd-kit/.git/member.py\n', 'git configuration component'),
    ('launchd-kit/member.pyc\n', 'bytecode path'),
])
def test_manifest_grammar_near_misses_refuse_by_name(tmp_path, text, reason):
    repo = build(tmp_path)
    (repo / '.job-host-manifest').write_bytes(text.encode())
    git(repo, 'add', '.job-host-manifest')
    result = run(repo)
    assert result.returncode == 1, f'manifest grammar accepted {text!r}'
    assert 'manifest grammar: ' + reason in result.stdout, result.stdout


def test_unstaged_manifest_cannot_hide_index_gap(tmp_path):
    repo = build(tmp_path)
    (repo / MEMBER).write_text('value = 2\n')
    git(repo, 'add', MEMBER)
    (repo / 'scripts/job-host-blobs.tsv').write_text('edited disk marker\n')
    result = run(repo)
    assert result.returncode == 1, 'disk marker overruled live index'
    assert 'changed from recorded source blob' in result.stdout


@pytest.mark.parametrize('change,reason', [
    ('header', 'pin grammar: invalid header'),
    ('row', 'pin grammar: invalid row'),
    ('order', 'pin grammar: paths not sorted and unique'),
])
def test_pin_grammar_near_misses_refuse(tmp_path, change, reason):
    repo = build(tmp_path)
    path = repo / 'scripts/job-host-blobs.tsv'
    text = path.read_text()
    if change == 'header':
        text = text.replace('Source commit', 'source commit')
    elif change == 'row':
        text = text.replace('\t', ' ')
    else:
        text += text.splitlines()[-1] + '\n'
    path.write_text(text)
    git(repo, 'add', 'scripts/job-host-blobs.tsv')
    result = run(repo)
    assert result.returncode == 1, 'pin grammar accepted malformed content'
    assert reason in result.stdout, result.stdout


def test_a_covered_tree_line_passes(tmp_path):
    repo = build(tmp_path)
    (repo / '.job-host-manifest').write_text('tree launchd-kit/\n' + MEMBER + '\n')
    git(repo, 'add', '.job-host-manifest')
    result = run(repo)
    assert result.returncode == 0, result.stdout


def test_concurrent_stage_keeps_snapshot_consistent_and_rerun_reads_new_index(tmp_path):
    repo = build(tmp_path)
    spec = importlib.util.spec_from_file_location('snapshot_manifest', repo / SCRIPT)
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    original_git = checker.git
    raced = []

    def stage_after_capture(args, root):
        result = original_git(args, root)
        if args[0] == 'ls-files' and not raced:
            def writer():
                (repo / MEMBER).write_text('value = 2\n')
                git(repo, 'add', MEMBER)
                raced.append(True)
            thread = Thread(target=writer)
            thread.start()
            thread.join()
            assert raced, 'concurrent writer did not stage its change'
        return result

    checker.git = stage_after_capture
    assert checker.check(repo) == [], 'concurrent stage changed the captured snapshot'
    assert checker.check(repo) == [
        f'{MEMBER}: changed from recorded source blob'
    ], 'rerun did not read the newly staged blob'
