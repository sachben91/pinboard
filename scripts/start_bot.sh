#!/usr/bin/env bash
set -e

# Run DB migration/init
python3 -c "
import sys; sys.path.insert(0, '.')
from src.pinboard.db import init_db
init_db()
print('DB ready.')
"

echo "==> Starting YCPin bot..."
exec python3 scripts/ycpin_bot.py
