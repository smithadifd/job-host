"""A stand-in resolver module for the import-path tests. Copied into a scratch
tree as launchd-kit/op-resolve.py, beside a copy of job-host/: jh_secrets.py
imports that file and calls its main(argv); this one records that it was
called, then execs the command the way op-resolve does."""
import os


def main(argv):
    with open(os.environ["STUB_RESOLVER_SENTINEL"], "w") as fh:
        fh.write(" ".join(argv))
    cmd = argv[argv.index("--") + 1:]
    env = dict(os.environ)
    env["STUB_RESOLVED"] = "from-stub"
    os.execvpe(cmd[0], cmd, env)
