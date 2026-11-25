#!/bin/sh
set -e

if [ "$(id -u)" = "0" ]; then
    # Bind-mounted folders (e.g. on Unraid) often belong to another user.
    chown -R "$PUID:$PGID" "$DATA_DIR"
    # Drop root and run the bot as PUID:PGID.
    exec python -c 'import os, sys
os.setgroups([])
os.setgid(int(os.environ["PGID"]))
os.setuid(int(os.environ["PUID"]))
os.execvp(sys.argv[1], sys.argv[1:])' "$@"
fi

exec "$@"
