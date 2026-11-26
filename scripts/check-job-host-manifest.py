#!/usr/bin/env python3
"""Check the staged carve manifest and immutable source blob pins, offline.

Adapted from harness-core's check-core-manifest.py: index-only inputs,
regular-file modes, strict manifest grammar, and no caller-selected root.
The leaf's two vendored roots replace the core's repository-only table;
files outside those roots are repository-owned. Blob pins add byte integrity.

Manifest grammar: UTF-8, LF-terminated, nonempty; optional nonoverlapping
``tree <folder>/`` lines before byte-sorted, unique, casefold-unique paths.
Components use [A-Za-z0-9._-], excluding dot components, git configuration,
bytecode and cache directories. Every path is in one of the two leaf roots.
Pin grammar: two fixed comment lines, then sorted ``path TAB git-blob``
rows with 40 lowercase hex digits. Its source header holds a commit SHA.
All inputs come from the current git index, never disk edits or tracking refs.
No files or refs are written, so failures and concurrent invocations are
repeatable; index entries bind all blob reads even if a writer stages later.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = '.job-host-manifest'
PINS = 'scripts/job-host-blobs.tsv'
ROOTS = ('job-host/', 'launchd-kit/')
PREFIX = 'job-host manifest:'
_COMPONENT = re.compile(r'[A-Za-z0-9._-]+')
_GIT_LOCATION_ENV = frozenset({
    'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY',
    'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_COMMON_DIR', 'GIT_NAMESPACE',
    'GIT_CEILING_DIRECTORIES', 'GIT_DISCOVERY_ACROSS_FILESYSTEM',
    'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT',
})


class CheckError(RuntimeError):
    """Unreadable state or malformed input; always a nonzero gate result."""


def git(args: list[str], root: Path) -> bytes:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_ENV
           and not k.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_'))}
    env['GIT_NO_REPLACE_OBJECTS'] = '1'
    try:
        done = subprocess.run(['git', *args], cwd=root, env=env, capture_output=True)
    except OSError as exc:
        raise CheckError('git unavailable') from exc
    if done.returncode:
        raise CheckError(f'git {args[0]} failed')
    return done.stdout


def tracked(root: Path) -> dict:
    if Path(git(['rev-parse', '--show-toplevel'], root).decode().strip()).resolve() != root.resolve():
        raise CheckError('not repository top')
    index = {}
    for record in git(['ls-files', '--stage', '-z'], root).split(b'\0'):
        if not record:
            continue
        meta, name = record.split(b'\t', 1)
        mode, blob, stage = meta.decode('ascii').split(' ')
        path = name.decode('utf-8', 'surrogateescape')
        if stage != '0' or path in index:
            raise CheckError('unmerged index')
        index[path] = (mode, blob)
    return index


def staged_text(root: Path, index: dict, name: str) -> str:
    if name not in index or index[name][0] != '100644':
        raise CheckError(f'{name}: not a tracked regular file')
    try:
        return git(['cat-file', 'blob', index[name][1]], root).decode('utf-8')
    except UnicodeError as exc:
        raise CheckError(f'{name}: not UTF-8') from exc


def path_problem(path: str) -> str | None:
    if not path.startswith(ROOTS):
        return 'outside vendored roots'
    for part in path.split('/'):
        if not _COMPONENT.fullmatch(part) or part in {'.', '..', '__pycache__'}:
            return 'invalid component'
        if part.casefold() in {'.git', '.gitattributes', '.gitmodules', '.gitignore'}:
            return 'git configuration component'
    if path.endswith('.pyc'):
        return 'bytecode path'
    return None


def parse_manifest(text: str) -> tuple[list[str], list[str]]:
    if not text or '\r' in text or not text.endswith('\n'):
        raise CheckError('manifest grammar: requires nonempty LF-terminated text')
    trees, paths, folded = [], [], set()
    for line in text[:-1].split('\n'):
        if line.startswith('tree '):
            folder = line[5:]
            if paths or not folder.endswith('/') or path_problem(folder[:-1] + '/member'):
                raise CheckError('manifest grammar: invalid tree line')
            if any(folder.startswith(t) or t.startswith(folder) for t in trees):
                raise CheckError('manifest grammar: overlapping tree')
            trees.append(folder)
            continue
        problem = path_problem(line)
        if problem:
            raise CheckError(f'manifest grammar: {problem}')
        if paths and line.encode() <= paths[-1].encode() or line.casefold() in folded:
            raise CheckError('manifest grammar: paths not sorted and unique')
        folded.add(line.casefold())
        paths.append(line)
    if not paths or any(not any(p.startswith(t) for p in paths) for t in trees):
        raise CheckError('manifest grammar: empty tree or member set')
    return trees, paths


def parse_pins(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if ('\r' in text or not text.endswith('\n') or len(lines) < 3
            or not re.fullmatch(r'# Source commit: [0-9a-f]{40}', lines[0])
            or lines[1] != '# path TAB immutable source git blob'):
        raise CheckError('pin grammar: invalid header')
    pins = {}
    for line in lines[2:]:
        fields = line.split('\t')
        if len(fields) != 2 or path_problem(fields[0]) or not re.fullmatch(r'[0-9a-f]{40}', fields[1]):
            raise CheckError('pin grammar: invalid row')
        path, blob = fields
        if pins and path.encode() <= next(reversed(pins)).encode():
            raise CheckError('pin grammar: paths not sorted and unique')
        pins[path] = blob
    return pins


def check(root: Path) -> list[str]:
    index = tracked(root)
    _, paths = parse_manifest(staged_text(root, index, MANIFEST))
    pins = parse_pins(staged_text(root, index, PINS))
    findings = []
    if set(paths) != set(pins):
        findings.append('manifest/pin member set differs')
    for path in paths:
        entry = index.get(path)
        if entry is None:
            findings.append(f'{path}: listed but not tracked')
        elif entry[0] != '100644':
            findings.append(f'{path}: listed at mode {entry[0]}')
        elif entry[1] != pins.get(path):
            findings.append(f'{path}: changed from recorded source blob')
    for path in sorted(index):
        if path.startswith(ROOTS) and path not in pins:
            findings.append(f'{path}: vendored file is extra')
    return findings


def main() -> int:
    try:
        if len(sys.argv) != 1:
            raise CheckError('takes no arguments')
        findings = check(ROOT)
    except CheckError as exc:
        print(f'{PREFIX} ERROR: {exc}')
        return 1
    for finding in findings:
        print(finding)
    if findings:
        print(f'{PREFIX} FAIL: {len(findings)} finding(s)')
        return 1
    print(f'{PREFIX} ok: all vendored paths, modes and source blobs match')
    return 0


if __name__ == '__main__':
    sys.exit(main())
