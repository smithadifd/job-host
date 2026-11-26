"""The imported identity gate checks each metadata field without revealing it."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = 'scripts/ci/check-commit-identity.sh'
BLOCKED = 'blocked-fixture@example.invalid'
FIELDS = {
    'author name': 'GIT_AUTHOR_NAME',
    'author email': 'GIT_AUTHOR_EMAIL',
    'committer name': 'GIT_COMMITTER_NAME',
    'committer email': 'GIT_COMMITTER_EMAIL',
}


@pytest.mark.parametrize('field', FIELDS)
def test_blocked_metadata_field_refuses_and_clean_metadata_passes(tmp_path, field):
    repo = tmp_path / 'repo'
    repo.mkdir()
    script = repo / SCRIPT
    script.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / SCRIPT, script)
    env = {
        'PATH': os.environ.get('PATH', ''), 'HOME': str(tmp_path),
        'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_AUTHOR_NAME': 'Fixture Author', 'GIT_AUTHOR_EMAIL': 'author@example.invalid',
        'GIT_COMMITTER_NAME': 'Fixture Committer', 'GIT_COMMITTER_EMAIL': 'committer@example.invalid',
        'BLOCKED_COMMIT_IDENTITY': BLOCKED,
    }

    def git(*args, data=None, identity=None):
        return subprocess.run(['git', *args], cwd=repo, env=identity or env,
                              input=data, capture_output=True, check=True).stdout.decode().strip()

    git('init', '-q')
    tree = git('hash-object', '-w', '-t', 'tree', '--stdin', data=b'')
    base = git('commit-tree', tree, '-m', 'fixture base')
    blocked_env = {**env, FIELDS[field]: BLOCKED.upper()}
    head = git('commit-tree', tree, '-p', base, '-m', 'fixture candidate', identity=blocked_env)
    result = subprocess.run(['bash', str(script), base, head], cwd=repo, env=env,
                            capture_output=True, text=True)
    assert result.returncode == 1, f'identity gate accepted blocked {field}'
    assert f'{field} matches a blocked identity' in result.stdout, result.stdout
    assert BLOCKED not in (result.stdout + result.stderr).lower(), 'identity value was disclosed'
    clean = git('commit-tree', tree, '-p', base, '-m', 'clean candidate')
    result = subprocess.run(['bash', str(script), base, clean], cwd=repo, env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, 'identity gate refused clean metadata: ' + result.stdout
