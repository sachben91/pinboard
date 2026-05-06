#!/usr/bin/env bash
# Startup script for Railway deployment.
# Installs rclone, syncs the Pinboard DB from Google Drive, then starts the bot.
set -e

DB_PATH="${PINBOARD_DB_PATH:-/data/pinboard.db}"
RCLONE_REMOTE="${RCLONE_REMOTE:-gdrive:pinboard/pinboard.db}"

echo "==> DB path: $DB_PATH"
mkdir -p "$(dirname "$DB_PATH")"

# Install rclone if not present
if ! command -v rclone &>/dev/null; then
    echo "==> Installing rclone..."
    curl -fsSL https://rclone.org/install.sh | bash 2>/dev/null || \
        (curl -O https://downloads.rclone.org/rclone-current-linux-amd64.zip && \
         unzip -q rclone-current-linux-amd64.zip && \
         mv rclone-*-linux-amd64/rclone /usr/local/bin/ && \
         rm -rf rclone-*)
fi

# Write rclone config from env var
if [ -n "$RCLONE_CONFIG_CONTENT" ]; then
    mkdir -p ~/.config/rclone
    printf '%s' "$RCLONE_CONFIG_CONTENT" > ~/.config/rclone/rclone.conf
    echo "==> rclone config written"
fi

# Download DB from GDrive
if command -v rclone &>/dev/null && [ -n "$RCLONE_REMOTE" ]; then
    echo "==> Syncing DB from $RCLONE_REMOTE..."
    rclone copyto "$RCLONE_REMOTE" "$DB_PATH" --retries 3 || echo "==> rclone sync failed, using existing DB"
fi

# Run migration in case schema is behind
python3 -c "
import sys; sys.path.insert(0, '.')
from src.pinboard.config import DB_PATH
from src.pinboard.db import init_db
init_db(DB_PATH)
print('DB ready.')
"

export PINBOARD_DB_PATH="$DB_PATH"

echo "==> Starting YCPin bot..."
exec python3 scripts/ycpin_bot.py
