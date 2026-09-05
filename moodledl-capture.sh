#!/bin/sh
# Handler for moodledl:// URLs, registered with the desktop for this user.
#
# Moodle's mobile-login flow ends by redirecting the browser to
#   moodledl://token=<base64>
# which no application can open. This script is that application: it does
# nothing but write the URL where moodle_sync.py login can read it.
#
# The captured URL contains a live web-service token, so the file is mode 600
# and moodle_sync.py deletes it as soon as the token is saved.

set -eu

dir="${XDG_CACHE_HOME:-$HOME/.cache}/hku-moodle"
umask 077
mkdir -p "$dir"
printf '%s\n' "${1:-}" > "$dir/token-url"

if command -v notify-send >/dev/null 2>&1; then
    notify-send -a "HKU Moodle sync" "Moodle token captured" \
        "Go back to the terminal and press Enter." 2>/dev/null || true
fi
