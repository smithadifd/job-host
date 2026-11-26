#!/usr/bin/env bash
# Fail when any commit in <base>..<head> uses a blocked author/committer identity.
# BLOCKED_COMMIT_IDENTITY is a comma-separated list of emails and/or names.
# Matches are case-insensitive exact per field. A hit prints only the commit
# SHA and which field matched — never the value. Unset or empty: warn and pass.
#
# xtrace is disabled for the whole process so `bash -x` or a caller `set -x`
# cannot echo the blocked list or a matched field.
set +xv
set -eu

usage() {
  echo "Usage: $0 <base> <head>" >&2
  exit 2
}

[ "$#" -eq 2 ] || usage
base=$1
head=$2

warn_unset() {
  msg='WARN: BLOCKED_COMMIT_IDENTITY is not set; commit identity not checked'
  if [ "${GITHUB_ACTIONS:-}" = true ]; then
    printf '%s\n' "::warning::$msg"
  else
    printf '%s\n' "$msg"
  fi
}

# Fail closed on an unreadable range even when the list is unset: a bad
# base, bad head, missing repo, or shallow clone that lacks the range
# must be nonzero. Only a readable range may take the warn-and-pass path.
git rev-parse --verify "${base}^{commit}" >/dev/null
git rev-parse --verify "${head}^{commit}" >/dev/null

# Unset, empty, or whitespace-only (including a list of empty comma fields)
# is a loud pass, never a silent one.
if [ -z "${BLOCKED_COMMIT_IDENTITY+x}" ] || [ -z "${BLOCKED_COMMIT_IDENTITY}" ]; then
  warn_unset
  exit 0
fi

trim() {
  set +xv
  _s=$1
  _s=${_s#"${_s%%[![:space:]]*}"}
  _s=${_s%"${_s##*[![:space:]]}"}
  printf '%s' "$_s"
}

lower() {
  set +xv
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

tokens=()
# IFS split only; do not echo the source string.
_rest=$BLOCKED_COMMIT_IDENTITY
while :; do
  set +xv
  case "$_rest" in
    *,*)
      _tok=${_rest%%,*}
      _rest=${_rest#*,}
      ;;
    *)
      _tok=$_rest
      _rest=
      ;;
  esac
  _tok=$(lower "$(trim "$_tok")")
  if [ -n "$_tok" ]; then
    tokens+=("$_tok")
  fi
  [ -n "$_rest" ] || break
done
_tok=
_rest=
_s=

if [ "${#tokens[@]}" -eq 0 ]; then
  warn_unset
  exit 0
fi

field_matches() {
  set +xv
  _fv=$(lower "$(trim "$1")")
  _t=
  for _t in "${tokens[@]}"; do
    if [ "$_fv" = "$_t" ]; then
      _fv=
      _t=
      return 0
    fi
  done
  _fv=
  _t=
  return 1
}

failed=0
# SHAs only on this list — safe to iterate. Capture rev-list first: a
# failing command substitution inside a heredoc is swallowed and would
# pass a range that was never listed.
if ! _revs=$(git -c log.showSignature=false rev-list "${base}..${head}"); then
  printf '%s\n' "error: git rev-list failed for ${base}..${head}" >&2
  exit 1
fi
while IFS= read -r fullsha; do
  set +xv
  [ -n "$fullsha" ] || continue
  short=$(git rev-parse --short "$fullsha")
  an=$(git log -1 --format='%an' "$fullsha")
  ae=$(git log -1 --format='%ae' "$fullsha")
  cn=$(git log -1 --format='%cn' "$fullsha")
  ce=$(git log -1 --format='%ce' "$fullsha")
  if field_matches "$an"; then
    printf '%s: author name matches a blocked identity\n' "$short"
    failed=1
  fi
  if field_matches "$ae"; then
    printf '%s: author email matches a blocked identity\n' "$short"
    failed=1
  fi
  if field_matches "$cn"; then
    printf '%s: committer name matches a blocked identity\n' "$short"
    failed=1
  fi
  if field_matches "$ce"; then
    printf '%s: committer email matches a blocked identity\n' "$short"
    failed=1
  fi
  an=
  ae=
  cn=
  ce=
done <<EOF
${_revs}
EOF
_revs=

if [ "$failed" -ne 0 ]; then
  exit 1
fi
exit 0
