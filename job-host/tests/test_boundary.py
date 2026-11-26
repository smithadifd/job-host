#!/usr/bin/env python3
"""BOUNDARY.md must map every shared shell function at its current source line."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _support import BASH, HOST_SH, JOB_HOST, LIB_SH  # noqa: E402

DESTINATIONS = {"job host", "notify integration", "Serotine payload", "fallback chain"}
FUNC_DEF = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{")
ROW = re.compile(
    r"^\| `([^`]+\.sh)` \| `([A-Za-z_][A-Za-z0-9_]*)` \| (\d+) \| ([^|]+?) \| ([^|]*?) \|"
)
SOURCES = [LIB_SH, LIB_SH.parent / "lib" / "notify.sh",
           LIB_SH.parent / "lib" / "chain.sh", LIB_SH.parent / "lib" / "telemetry.sh"]


def source_functions():
    found = {}
    for source in SOURCES:
        for n, line in enumerate(source.read_text().splitlines(), 1):
            m = FUNC_DEF.match(line)
            if m:
                found[(str(source.relative_to(LIB_SH.parent.parent)), m.group(1))] = n
    return found


def boundary_rows():
    rows = []
    for line in (JOB_HOST / "BOUNDARY.md").read_text().splitlines():
        m = ROW.match(line)
        if m:
            rows.append((m.group(1), m.group(2), int(m.group(3)),
                         m.group(4).strip(), m.group(5).strip()))
    return rows


def host_functions():
    proc = subprocess.run(
        [BASH, "-c", 'source "$0" && declare -F', str(HOST_SH)],
        stdout=subprocess.PIPE, universal_newlines=True, check=True,
    )
    return {line.split()[-1] for line in proc.stdout.splitlines()}


class BoundaryTest(unittest.TestCase):

    def test_every_source_function_is_mapped_once_at_its_line(self):
        rows = boundary_rows()
        keys = [(r[0], r[1]) for r in rows]
        self.assertEqual(len(keys), len(set(keys)), "a function is mapped twice")
        self.assertEqual({(r[0], r[1]): r[2] for r in rows}, source_functions())

    def test_destinations_are_the_four_named_ones(self):
        for _, name, _, dest, _ in boundary_rows():
            self.assertIn(dest, DESTINATIONS, name)

    def test_job_host_rows_name_an_existing_counterpart(self):
        defined = host_functions()
        for _, name, _, dest, counterpart in boundary_rows():
            if dest != "job host":
                continue
            m = re.match(r"`([A-Za-z_][A-Za-z0-9_]*)`", counterpart)
            self.assertIsNotNone(m, "%s maps to the job host without a counterpart" % name)
            self.assertIn(m.group(1), defined, name)

    def test_lib_names_of_job_host_functions_are_defined_by_host_sh(self):
        defined = host_functions()
        for _, name, _, dest, _ in boundary_rows():
            if dest == "job host" and not name.startswith("_lk_run_exit") \
                    and name not in ("_lk_slot_claim_on_success", "_lk_release_lock"):
                self.assertIn(name, defined, "host.sh lacks the lib.sh name %s" % name)

    def test_split_integration_functions_are_loaded_by_host_sh(self):
        defined = host_functions()
        for source, name, _, dest, _ in boundary_rows():
            if dest != "job host":
                self.assertIn(name, defined, "host.sh did not load %s from %s" % (name, source))

    def test_function_names_do_not_collide_across_source_files(self):
        names = [name for _, name in source_functions()]
        self.assertEqual(len(names), len(set(names)), "split files redefine a function name")


if __name__ == "__main__":
    unittest.main(verbosity=2)
