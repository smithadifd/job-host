"""Closure exceptions are exact, path-scoped, and index-bound."""
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HEADER = '# path\tsha256 of lowercased matched reference\treason\n'
URL = 'https://github.com/example/claude' + '-toolkit/schema.json'
REPO_URL = 'https://github.com/example/claude' + '-toolkit'


def git(repo, *args):
    subprocess.run(['git', *args], cwd=repo, capture_output=True, check=True)


def build(tmp_path, text='clean\n', rows=''):
    repo = tmp_path / 'repo'
    (repo / 'scripts').mkdir(parents=True)
    for name in ('check-job-host-manifest.py', 'check-closure.py'):
        shutil.copyfile(ROOT / 'scripts' / name, repo / 'scripts' / name)
    (repo / 'scripts/closure-allowance.tsv').write_text(HEADER + rows)
    (repo / 'notes.md').write_text(text)
    git(repo, 'init', '-q')
    git(repo, 'add', '-A')
    return repo


def run(repo):
    return subprocess.run([sys.executable, '-B', '-I', str(repo / 'scripts/check-closure.py')],
                          cwd=repo, capture_output=True, text=True)


@pytest.mark.parametrize('text', [
    '/Us' + 'ers/example/project', '/ho' + 'me/example/project',
    'C:\\Us' + 'ers\\example\\project', '~' + '/project/file',
    'claude' + '-toolkit/scripts/file', '/opt/claude' + '-toolkit', URL,
    REPO_URL, REPO_URL + '#readme', REPO_URL + '?tab=readme-ov-file', REPO_URL + '.git#readme',
    'see ' + REPO_URL + '.', 'see ' + REPO_URL + ', then', '(' + REPO_URL + ')',
])
def test_home_and_toolkit_references_refuse(tmp_path, text):
    repo = build(tmp_path, text + '\n')
    # An unstaged cleanup cannot hide what will be published.
    (repo / 'notes.md').write_text('clean\n')
    result = run(repo)
    assert result.returncode == 1, 'closure accepted a forbidden reference'
    assert 'closure: notes.md:1: forbidden reference' in result.stdout, result.stdout


def test_clean_tree_and_portable_home_variable_pass(tmp_path):
    result = run(build(tmp_path, '$HOME/.local/share/job-host\n'))
    assert result.returncode == 0, result.stdout
    assert 'closure: ok:' in result.stdout


def test_exception_is_exact_and_path_scoped(tmp_path):
    value = hashlib.sha256(URL.lower().encode()).hexdigest()
    row = f'notes.md\t{value}\tpublic schema annotation\n'
    repo = build(tmp_path, URL + '\n', row)
    result = run(repo)
    assert result.returncode == 0, result.stdout
    (repo / 'other.md').write_text(URL + '\n')
    git(repo, 'add', '-A')
    result = run(repo)
    assert result.returncode == 1, 'closure exception escaped its path'
    assert 'closure: other.md:1: forbidden reference' in result.stdout


def test_unused_exception_refuses(tmp_path):
    row = 'notes.md\t' + hashlib.sha256(URL.encode()).hexdigest() + '\tpublic URL\n'
    result = run(build(tmp_path, rows=row))
    assert result.returncode == 1, 'closure accepted stale exception'
    assert 'closure: notes.md: stale allowance' in result.stdout


@pytest.mark.parametrize('row,reason', [
    ('notes.md ' + 'a' * 64 + ' reason\n', 'invalid row'),
    ('notes.md\t' + 'A' * 64 + '\treason\n', 'invalid row'),
    ('notes.md\t' + 'a' * 64 + '\t\n', 'invalid row'),
    ('../notes.md\t' + 'a' * 64 + '\treason\n', 'invalid row'),
    (('notes.md\t' + 'a' * 64 + '\treason\n') * 2, 'duplicate row'),
])
def test_allowance_near_misses_refuse(tmp_path, row, reason):
    result = run(build(tmp_path, rows=row))
    assert result.returncode == 1, 'closure accepted malformed exception'
    assert 'closure allowance grammar: ' + reason in result.stdout


def test_exception_does_not_allow_a_different_reference_in_the_same_file(tmp_path):
    row = 'notes.md\t' + hashlib.sha256(URL.encode()).hexdigest() + '\tpublic URL\n'
    repo = build(tmp_path, URL + '\n' + URL + '/different\n', row)
    result = run(repo)
    assert result.returncode == 1, 'closure exception escaped its exact reference'
    assert 'closure: notes.md:2: forbidden reference' in result.stdout


def test_regex_source_and_repository_mentions_are_not_paths(tmp_path):
    text = 'a claude' + '-toolkit mention; regex: ' + '~' + '/(?:pattern)\n'
    result = run(build(tmp_path, text))
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize('text', [
    'https://github.com/example/claude' + '-toolkit-extras',
    'https://github.com/example/claude' + '-toolkitx',
])
def test_a_longer_repository_name_is_not_the_toolkit(tmp_path, text):
    result = run(build(tmp_path, text + '\n'))
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize('header', [
    HEADER.upper(), HEADER.replace('\t', ' '), '- ' + HEADER,
    '```\n' + HEADER + '```\n', '', HEADER.rstrip('\n'), HEADER.replace('\n', '\r\n'),
])
def test_allowance_header_near_misses_refuse(tmp_path, header):
    repo = build(tmp_path)
    (repo / 'scripts/closure-allowance.tsv').write_bytes(header.encode())
    git(repo, 'add', 'scripts/closure-allowance.tsv')
    result = run(repo)
    assert result.returncode == 1, 'closure accepted malformed allowance header'
    assert 'closure allowance grammar: invalid header' in result.stdout, result.stdout
