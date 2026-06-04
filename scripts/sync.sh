#!/bin/bash
# sync.sh — daily sync from quant_sys PG → stock_data CSV → git push
# Run by cron: 0 18 * * 1-5 cd ~/stock_data && bash scripts/sync.sh

set -e
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
QUANT_DIR="$HOME/workspace/quant_sys"
VENV_PYTHON="$QUANT_DIR/.venv/bin/python"

cd "$REPO_DIR"

echo "=== $(date) ==="

# 1. Export today's data
echo "[1/4] Exporting today's data..."
$VENV_PYTHON -B "$REPO_DIR/scripts/export.py" 2>&1

# 2. Check for changes
if git diff --quiet && git diff --cached --quiet; then
    echo "[2/4] No changes, skipping commit."
    exit 0
fi

# 3. Commit
echo "[2/4] Changes detected:"
git diff --stat
git diff --stat --cached

TODAY=$(date +%Y-%m-%d)
git add data/
git commit -m "data: $TODAY update

$(git diff --cached --stat | tail -1)"

# 4. Push
echo "[3/4] Pushing..."
git push origin main

echo "[4/4] Done ✅"
