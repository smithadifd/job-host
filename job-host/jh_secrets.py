#!/usr/bin/env python3
"""jh_secrets.py — the job host's secrets step: resolve, then exec.

    python3 job-host/jh_secrets.py --env-file FILE -- cmd args...

This is a thin wrapper around launchd-kit/op-resolve.py, which it IMPORTS
(the resolver is not copied here). op-resolve reads FILE, resolves every
op:// value against 1Password Connect, and execs the command only when every
reference resolved, so the command never starts with a partial environment
and the Connect credentials never reach it.

What the wrapper adds is a bound on the resolution step:
JH_SECRETS_TIMEOUT_SECS (default 120). Resolution runs in a forked child
that leads its own process group. op-resolve's main() runs there with its
final exec intercepted: instead of starting the command, the child hands the
resolved (file, argv, env) back over a pipe and exits. If that has not
happened by the bound, the whole group is killed (the child and anything it
started, such as a hung Keychain `security` lookup), this process exits 124
and the command never starts. Otherwise this process execs the command
itself, so the command's own runtime is not limited by the bound, and signals
and the exit status pass through as they do with op-resolve.

If this process is terminated while resolution is running, it kills the
group first (SIGTERM, SIGINT, SIGHUP); if it dies outright, the child notices
the closed pipe and kills its own group.

The module is always ../launchd-kit/op-resolve.py relative to this file; no
environment variable can point the import elsewhere. A module that cannot be
loaded, or that has no main(), is a hard failure (exit 1) — never a fallback
that runs the command without its secrets.

Secret values are never printed: all messages come from op-resolve, which
names references and keys only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import select
import signal
import sys
import threading
import time

DEFAULT_TIMEOUT_SECS = 120
TIMEOUT_EXIT = 124
FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RESOLVER = os.path.join(_HERE, os.pardir, "launchd-kit", "op-resolve.py")


def _fail(msg, code=1):
    sys.stderr.write("jh_secrets: " + msg + "\n")
    sys.exit(code)


def load_resolver(path):
    """Import the resolver module from `path` (its file name has a hyphen, so
    it cannot be imported by name)."""
    if not os.path.isfile(path):
        _fail("resolver module not found: %s — command NOT run" % path)
    spec = importlib.util.spec_from_file_location("jh_op_resolve", path)
    if spec is None or spec.loader is None:
        _fail("cannot load resolver module: %s — command NOT run" % path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - any import failure is fatal here
        _fail(
            "resolver module failed to import (%s) — command NOT run"
            % type(exc).__name__
        )
    if not callable(getattr(module, "main", None)):
        _fail("resolver module has no main() — command NOT run")
    return module


def _timeout_secs():
    raw = os.environ.get("JH_SECRETS_TIMEOUT_SECS", "")
    if raw == "":
        return DEFAULT_TIMEOUT_SECS
    if not raw.isdigit() or int(raw) <= 0:
        _fail(
            "JH_SECRETS_TIMEOUT_SECS must be a positive integer, got %r — "
            "command NOT run" % raw,
            2,
        )
    return int(raw)


class _Resolved(BaseException):
    """Raised in place of the resolver's exec; a BaseException so no
    `except Exception` in the resolver can swallow it."""


def _kill_group(pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def _watch_parent(guard_fd):
    # The parent holds the write end. EOF means the parent is gone: take the
    # whole group down rather than leave it running unbounded.
    try:
        while os.read(guard_fd, 1):
            pass
    except OSError:
        pass
    _kill_group(0)
    os._exit(1)


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _child(resolver, argv, result_fd, guard_fd):
    """Run the resolver; send back what it would have exec'd. Never returns."""
    code = 1
    try:
        threading.Thread(target=_watch_parent, args=(guard_fd,), daemon=True).start()

        def _capture_exec(file, args, env):
            raise _Resolved(file, list(args), dict(env))

        os.execvpe = _capture_exec
        try:
            resolver.main(argv)
        except _Resolved as done:
            file, args, env = done.args
            _write_all(result_fd, json.dumps([file, args, env]).encode("utf-8"))
            code = 0
        except SystemExit as exc:
            if exc.code is None or exc.code == 0:
                code = 0           # no payload: the parent reports it
            elif isinstance(exc.code, int):
                code = exc.code
            else:
                sys.stderr.write("%s\n" % (exc.code,))
                code = 1
        else:
            code = 0               # returned without exec: no payload
    except BaseException as exc:  # noqa: BLE001 - never return into the caller
        sys.stderr.write(
            "jh_secrets: resolver failed (%s) — command NOT run\n" % type(exc).__name__
        )
        code = 1
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            os._exit(code)


def _timed_out(pid):
    _kill_group(pid)
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass
    sys.stderr.write(
        "jh_secrets: secrets resolution did not finish within the bound — "
        "command NOT run\n"
    )
    sys.stderr.flush()
    os._exit(TIMEOUT_EXIT)


def resolve(resolver, argv, secs):
    """Run resolver.main(argv) in its own process group, bounded to `secs`.
    Return the (file, args, env) it would have exec'd, or exit non-zero."""
    result_r, result_w = os.pipe()
    guard_r, guard_w = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        try:
            os.setpgid(0, 0)
        except OSError:
            pass
        os.close(result_r)
        os.close(guard_w)
        _child(resolver, argv, result_w, guard_r)
    os.close(result_w)
    os.close(guard_r)
    try:
        os.setpgid(pid, pid)   # also set by the child; whichever runs first
    except OSError:
        pass

    def _forward(signum, frame):
        _kill_group(pid)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    previous = {s: signal.signal(s, _forward) for s in FORWARDED_SIGNALS}
    try:
        deadline = time.monotonic() + secs
        chunks = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _timed_out(pid)
            ready, _, _ = select.select([result_r], [], [], remaining)
            if not ready:
                _timed_out(pid)
            data = os.read(result_r, 65536)
            if not data:
                break
            chunks.append(data)
        while True:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            if time.monotonic() >= deadline:
                _timed_out(pid)
            time.sleep(0.01)
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)
        os.close(result_r)
        os.close(guard_w)

    if os.WIFSIGNALED(status):
        _fail("resolver was killed by signal %d — command NOT run" % os.WTERMSIG(status))
    code = os.WEXITSTATUS(status)
    if code != 0:
        sys.exit(code)     # the resolver already said why
    if not chunks:
        _fail("resolver returned without running the command")
    try:
        file, args, env = json.loads(b"".join(chunks).decode("utf-8"))
    except (ValueError, TypeError):
        _fail("resolver handed back an unreadable result — command NOT run")
    return file, args, env


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    secs = _timeout_secs()
    resolver = load_resolver(DEFAULT_RESOLVER)
    file, args, env = resolve(resolver, argv, secs)
    try:
        os.execvpe(file, args, env)
    except OSError as exc:
        _fail("cannot exec %r: %s" % (file, exc.strerror or exc), 127)


if __name__ == "__main__":
    main()
