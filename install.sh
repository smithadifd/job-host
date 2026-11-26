#!/bin/bash
# Copy the library tree; never activate scheduled jobs.
set -euo pipefail
if [[ $# -ne 1 ]]; then
    printf 'usage: bash install.sh DEST\n' >&2
    exit 2
fi
source_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
dest=$1
if [[ -e "$dest" || -L "$dest" ]]; then
    printf 'install: destination already exists: %s\n' "$dest" >&2
    exit 2
fi
mkdir -- "$dest"
cp -R -- "$source_root/launchd-kit" "$source_root/job-host" "$dest/"
printf 'installed libraries in %s; no jobs activated\n' "$dest"
