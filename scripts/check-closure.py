#!/usr/bin/env python3
# Matches absolute macOS/Linux/Windows user-home paths, tilde-home paths,
# and the toolkit repository name as a path component (including GitHub URLs,
# with or without a fragment, query or trailing punctuation).
# Generic $HOME expressions remain portable runtime defaults, not user paths.
# Exceptions pin the exact lowercased reference by SHA-256 and tracked path;
# a reason is mandatory, and an unused exception fails closed.
"""Read-only, index-bound closure check; no options or caller-selected root.

Allowance grammar: one fixed header, then path TAB sha256 TAB reason rows;
LF-terminated UTF-8, no duplicates, empty fields, dot/absolute paths or CR.
Only a complete matched reference can be allowed, never the surrounding line.
Concurrent runs write nothing; a rerun re-reads the index and immutable blobs.
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('leaf_manifest', ROOT / 'scripts/check-job-host-manifest.py')
manifest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manifest)
ALLOWANCE = 'scripts/closure-allowance.tsv'
HEADER = '# path\tsha256 of lowercased matched reference\treason'
TOOLKIT = 'claude' + '-toolkit'
REFERENCE = re.compile(
    r'/(?:Us' + r'ers|ho' + r'me)/[^/\s`\"\'<>)]+' 
    + r'|[A-Z]:[\\/](?:Us' + r'ers)[\\/][^\\/\s`\"\'<>)]+'
    + r'|~[/\\][A-Za-z0-9_.-][^\s`\"\'<>()[\]]*'
    + r'|(?<![A-Za-z0-9_.-])(?:[^\s`\"\'<>()[\]]*[/\\])' + TOOLKIT
    + r'(?:\.git)?(?![A-Za-z0-9_-])(?:[/\\#?][^\s`\"\'<>()[\]]*)?'
    + r'|(?<![A-Za-z0-9_.-])' + TOOLKIT + r'(?:\.git)?[/\\][^\s`\"\'<>()[\]]*',
    re.IGNORECASE,
)


def digest(text: str) -> str:
    return hashlib.sha256(text.lower().encode('utf-8', 'surrogateescape')).hexdigest()


def parse_allowance(text: str) -> dict:
    lines = text.splitlines()
    if not text.endswith('\n') or '\r' in text or not lines or lines[0] != HEADER:
        raise manifest.CheckError('closure allowance grammar: invalid header')
    allowed = {}
    for line in lines[1:]:
        fields = line.split('\t')
        if (len(fields) != 3 or not re.fullmatch(r'[0-9a-f]{64}', fields[1])
                or not fields[2].strip() or fields[0].startswith('/')
                or any(p in {'', '.', '..'} for p in fields[0].split('/'))):
            raise manifest.CheckError('closure allowance grammar: invalid row')
        pair = tuple(fields[:2])
        if pair in allowed:
            raise manifest.CheckError('closure allowance grammar: duplicate row')
        allowed[pair] = fields[2]
    return allowed


def check(root: Path) -> list[str]:
    index = manifest.tracked(root)
    allowed = parse_allowance(manifest.staged_text(root, index, ALLOWANCE))
    used, findings = set(), []
    for path, (mode, blob) in index.items():
        if REFERENCE.search(path):
            findings.append('closure: path name reference (redacted)')
        if mode not in {'100644', '100755', '120000'}:
            findings.append('closure: unreadable tracked entry')
            continue
        data = manifest.git(['cat-file', 'blob', blob], root).decode('utf-8', 'surrogateescape')
        for number, line in enumerate(data.split('\n'), 1):
            for match in REFERENCE.finditer(line):
                pair = (path, digest(match.group(0)))
                if pair in allowed:
                    used.add(pair)
                else:
                    findings.append(f'closure: {path}:{number}: forbidden reference')
    for pair in allowed.keys() - used:
        findings.append(f'closure: {pair[0]}: stale allowance')
    return findings


def main() -> int:
    try:
        if len(sys.argv) != 1:
            raise manifest.CheckError('takes no arguments')
        findings = check(ROOT)
    except manifest.CheckError as exc:
        print(f'closure: ERROR: {exc}')
        return 1
    for finding in findings:
        print(finding)
    if findings:
        print(f'closure: FAIL: {len(findings)} finding(s)')
        return 1
    print('closure: ok: tracked paths and blobs are closed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
