#!/usr/bin/env sh
# Pre-push leak scan (POSIX). Exit 1 blocks the push.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=${1:-$(CDPATH= cd -- "$here/../.." && pwd)}
exec python "$here/scan-sensitive-data.py" --repo "$repo" --scope all
