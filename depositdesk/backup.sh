#!/bin/sh
set -eu
umask 077
desk_script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$desk_script_dir/backup.py" "$@"
