#!/usr/bin/env bash
# install-op-resolve.sh — the single owner of the machine-local op-resolve.py.
#
# `launchd-kit/op-resolve.py` in this checkout is canonical. Consumers, however,
# must reach a resolver at a stable machine path that survives the checkout
# being moved, renamed or absent — a launchd job's ProgramArguments cannot
# depend on where someone happens to have cloned this repo. That stable path is
#
#     $HOME/.claude/scripts/op-resolve.py            (the "mirror")
#
# and this script is the ONLY thing that is allowed to write it. Everything
# else reads it. Keeping one writer is the whole point: a mirror refreshed by
# hand drifts from the canonical source silently, and a secrets-adjacent helper
# that has silently drifted is the worst possible thing to be running under a
# scheduled job nobody is watching.
#
# Contract
# --------
#   --check              Read-only. Reports the mirror's state and writes
#                        nothing, ever. Exit 0 in sync, 1 not in sync
#                        (missing or drifted), 2 could not verify.
#   --install            Stage the canonical bytes into the mirror. A mirror
#                        that already matches is left alone (idempotent). A
#                        DRIFTED mirror is REPORTED and then REFUSED — the
#                        replacement does not happen without --force, so drift
#                        is never discovered after the fact.
#   --install --force    Same, but after the drift report the drifted copy is
#                        preserved beside the mirror and the canonical bytes
#                        are staged over it. The report is always emitted
#                        BEFORE anything is written.
#
#   --source PATH        Override the canonical source (default: op-resolve.py
#                        beside this script).
#   --target PATH        Override the mirror (default:
#                        $HOME/.claude/scripts/op-resolve.py).
#
# What "in sync" means here is byte identity, not "close enough": the staged
# mirror is compared to the canonical source with cmp(1), and the reports carry
# both SHA-256 digests so a human reviewing a drift report can see at a glance
# which copy they are looking at.
#
# This script never reads, prints or logs the CONTENT of either file — the
# resolver is not itself a secret, but it sits directly in front of ones, and
# the habit of never echoing its body is cheap to keep.
#
# Usage:
#   bash launchd-kit/install-op-resolve.sh --check
#   bash launchd-kit/install-op-resolve.sh --install
#   bash launchd-kit/install-op-resolve.sh --install --force

set -uo pipefail

_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PREFIX="op-resolve mirror:"

MODE=""
FORCE=0
SOURCE="$_HERE/op-resolve.py"
TARGET=""

usage() {
  cat >&2 <<'USAGE'
Usage: install-op-resolve.sh (--check | --install [--force])
                             [--source PATH] [--target PATH]

  --check     read-only state report (exit 0 in sync, 1 not in sync, 2 unverifiable)
  --install   stage the canonical bytes into the mirror
  --force     with --install, replace a DRIFTED mirror after reporting it
USAGE
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --check)   [ -z "$MODE" ] || usage; MODE=check ;;
    --install) [ -z "$MODE" ] || usage; MODE=install ;;
    --force)   FORCE=1 ;;
    --source)  shift; [ $# -gt 0 ] || usage; SOURCE="$1" ;;
    --target)  shift; [ $# -gt 0 ] || usage; TARGET="$1" ;;
    -h|--help) usage ;;
    *)         echo "$PREFIX unknown argument: $1" >&2; usage ;;
  esac
  shift
done

[ -n "$MODE" ] || usage
if [ "$MODE" = check ] && [ "$FORCE" -eq 1 ]; then
  echo "$PREFIX --force is meaningless with --check (a check never writes)" >&2
  exit 2
fi

# The default mirror path. Resolved here rather than at the top so that a
# --target override never even computes a path under $HOME.
if [ -z "$TARGET" ]; then
  if [ -z "${HOME:-}" ]; then
    echo "$PREFIX cannot verify: HOME is unset and no --target was given" >&2
    exit 2
  fi
  TARGET="$HOME/.claude/scripts/op-resolve.py"
fi

# --- digests ---------------------------------------------------------------- #
# shasum(1) ships with macOS; sha256sum is the GNU/Linux name. Either produces
# "<hex>  <path>"; we keep only the hex.
digest() {
  local path=$1 out=""
  if command -v shasum >/dev/null 2>&1; then
    out="$(shasum -a 256 "$path" 2>/dev/null)" || return 1
  elif command -v sha256sum >/dev/null 2>&1; then
    out="$(sha256sum "$path" 2>/dev/null)" || return 1
  else
    return 1
  fi
  printf '%s\n' "${out%% *}"
}

# --- the canonical source must exist and be an ordinary readable file ------- #
if [ ! -f "$SOURCE" ] || [ ! -r "$SOURCE" ]; then
  echo "$PREFIX cannot verify: canonical source is missing or unreadable ($SOURCE)" >&2
  exit 2
fi
SOURCE_SHA="$(digest "$SOURCE")" || {
  echo "$PREFIX cannot verify: no usable SHA-256 tool (shasum or sha256sum) on PATH" >&2
  exit 2
}
echo "$PREFIX source $SOURCE sha256 $SOURCE_SHA"

# --- classify the mirror ----------------------------------------------------- #
# Refuse outright on anything that is not a plain regular file. A symlink at the
# mirror path would be followed on write and would silently redirect the staged
# bytes somewhere this script does not own; a directory or device node is a
# machine in a state no automatic replacement should be guessing at.
STATE=""
TARGET_SHA=""
if [ -L "$TARGET" ]; then
  echo "$PREFIX cannot verify: $TARGET is a symlink — refusing to read through or replace it" >&2
  exit 2
elif [ -e "$TARGET" ] && [ ! -f "$TARGET" ]; then
  echo "$PREFIX cannot verify: $TARGET exists but is not a regular file — refusing to replace it" >&2
  exit 2
elif [ ! -e "$TARGET" ]; then
  STATE=missing
elif [ ! -r "$TARGET" ]; then
  echo "$PREFIX cannot verify: $TARGET exists but is unreadable" >&2
  exit 2
elif cmp -s "$SOURCE" "$TARGET"; then
  STATE=in-sync
  TARGET_SHA="$SOURCE_SHA"
else
  STATE=drift
  TARGET_SHA="$(digest "$TARGET")" || TARGET_SHA="<undigestible>"
fi

case "$STATE" in
  in-sync) echo "$PREFIX target $TARGET in-sync sha256 $TARGET_SHA" ;;
  missing) echo "$PREFIX target $TARGET missing" ;;
esac

# The drift report. It is emitted here — before ANY branch that could write —
# so that no code path can replace a drifted mirror without the operator having
# been told first, not even one that fails partway through the replacement.
report_drift() {
  echo "$PREFIX DRIFT — $TARGET differs from the canonical source" >&2
  echo "$PREFIX   canonical sha256 $SOURCE_SHA ($SOURCE)" >&2
  echo "$PREFIX   mirror    sha256 $TARGET_SHA ($TARGET)" >&2
}

if [ "$STATE" = drift ]; then
  report_drift
fi

# --- --check: read-only, stops here ------------------------------------------ #
if [ "$MODE" = check ]; then
  case "$STATE" in
    in-sync) exit 0 ;;
    missing) echo "$PREFIX not in sync: the mirror is absent; run --install" >&2; exit 1 ;;
    drift)   echo "$PREFIX not in sync: run --install --force to replace the drifted mirror" >&2; exit 1 ;;
  esac
fi

# --- --install ---------------------------------------------------------------- #
if [ "$STATE" = in-sync ]; then
  echo "$PREFIX already in sync — nothing to do"
  exit 0
fi

if [ "$STATE" = drift ] && [ "$FORCE" -eq 0 ]; then
  echo "$PREFIX refusing to replace a drifted mirror without --force" >&2
  exit 1
fi

TARGET_DIR="$(dirname "$TARGET")"
if [ ! -d "$TARGET_DIR" ]; then
  if ! mkdir -p "$TARGET_DIR"; then
    echo "$PREFIX cannot verify: could not create $TARGET_DIR" >&2
    exit 2
  fi
fi

# Preserve whatever the drifted mirror was before overwriting it. A drift means
# someone (or something) put bytes there that this repo did not, and those
# bytes are evidence — the only copy of them, usually.
if [ "$STATE" = drift ]; then
  BACKUP="$TARGET.drift-$(date -u +%Y%m%dT%H%M%SZ)"
  if ! cp -p "$TARGET" "$BACKUP" 2>/dev/null; then
    echo "$PREFIX cannot verify: could not preserve the drifted mirror at $BACKUP — nothing was replaced" >&2
    exit 2
  fi
  echo "$PREFIX drifted mirror preserved at $BACKUP" >&2
fi

# Stage atomically: write a sibling temp file, then rename over the target, so
# a consumer reading the mirror concurrently sees either the whole old file or
# the whole new one and never a half-written resolver.
TMP="$(mktemp "$TARGET_DIR/.op-resolve.XXXXXX" 2>/dev/null)" || {
  echo "$PREFIX cannot verify: could not create a staging file in $TARGET_DIR — nothing was replaced" >&2
  exit 2
}
if ! cat "$SOURCE" > "$TMP" 2>/dev/null; then
  rm -f "$TMP"
  echo "$PREFIX cannot verify: could not write the staging file — nothing was replaced" >&2
  exit 2
fi
# The mirror is documented as directly invocable (`op-resolve.py --env-file …`),
# so it is staged executable regardless of the source file's own mode.
chmod 0755 "$TMP" 2>/dev/null || true
if ! mv -f "$TMP" "$TARGET" 2>/dev/null; then
  rm -f "$TMP"
  echo "$PREFIX cannot verify: could not move the staged file into place — nothing was replaced" >&2
  exit 2
fi

# Verify what actually landed rather than trusting that the copy did what it
# was told: the claim this script makes is byte identity, so it re-reads.
if ! cmp -s "$SOURCE" "$TARGET"; then
  echo "$PREFIX FAILED: the staged mirror is not byte-identical to $SOURCE" >&2
  exit 2
fi
echo "$PREFIX staged $TARGET sha256 $SOURCE_SHA"
exit 0
