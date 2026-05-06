#!/usr/bin/env bash
# Startup script for Railway deployment.
# Syncs the Pinboard DB from Google Drive via rclone, then starts the bot.
set -e

DB_PATH="${PINBOARD_DB_PATH:-/data/pinboard.db}"
RCLONE_REMOTE="${RCLONE_REMOTE:-gdrive:pinboard/pinboard.db}"

echo "==> DB path: $DB_PATH"
mkdir -p "$(dirname "$DB_PATH")"

# Write rclone config from env var if provided
if [ -n "$RCLONE_CONFIG_CONTENT" ]; then
    mkdir -p ~/.config/rclone
    echo "$RCLONE_CONFIG_CONTENT" > ~/.config/rclone/rclone.conf
    echo "==> rclone config written from env"
fi

# Download DB from GDrive if rclone is available and DB doesn't exist / is stale
if command -v rclone &>/dev/null && [ -n "$RCLONE_REMOTE" ]; then
    echo "==> Syncing DB from $RCLONE_REMOTE..."
    rclone copyto "$RCLONE_REMOTE" "$DB_PATH" --retries 3 || echo "==> rclone sync failed, using existing DB"
else
    echo "==> rclone not available, using existing DB"
fi

export PINBOARD_DB_PATH="$DB_PATH"

echo "==> Starting YCPin bot..."
exec python3 scripts/ycpin_bot.py
