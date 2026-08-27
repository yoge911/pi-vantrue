#!/bin/bash

set -e

APP_DIR="/home/picar/vantrue-automation/pi-vantrue"
BRANCH="main"
IPHONE_CONNECTION="iPhone"

MAX_WAIT_SECONDS=40
RETRY_INTERVAL=5

echo "[Updater] Starting boot update process..."

# ---------------------------------------------------------
# 1. Try to connect to iPhone hotspot for up to 40 seconds
# ---------------------------------------------------------

echo "[Updater] Waiting for iPhone hotspot..."

elapsed=0
iphone_connected=false

while [ "$elapsed" -lt "$MAX_WAIT_SECONDS" ]; do

    if nmcli connection up "$IPHONE_CONNECTION" >/dev/null 2>&1; then
        echo "[Updater] Connected to iPhone hotspot."
        iphone_connected=true
        break
    fi

    echo "[Updater] iPhone hotspot not available yet. Retrying..."

    sleep "$RETRY_INTERVAL"
    elapsed=$((elapsed + RETRY_INTERVAL))
done

# ---------------------------------------------------------
# 2. If iPhone was not found, skip Git update
# ---------------------------------------------------------

if [ "$iphone_connected" = false ]; then
    echo "[Updater] iPhone hotspot unavailable after ${MAX_WAIT_SECONDS}s."
    echo "[Updater] Skipping repository update."
    exit 0
fi

# ---------------------------------------------------------
# 3. Check GitHub availability
# ---------------------------------------------------------

cd "$APP_DIR"

echo "[Updater] Checking Git repository..."

if ! git ls-remote origin >/dev/null 2>&1; then
    echo "[Updater] Git repository is unreachable."
    echo "[Updater] Skipping update."
    exit 0
fi

# ---------------------------------------------------------
# 4. Protect against local modifications
# ---------------------------------------------------------

if [ -n "$(git status --porcelain)" ]; then
    echo "[Updater] Local repository contains uncommitted changes."
    echo "[Updater] Update aborted."
    exit 1
fi

# ---------------------------------------------------------
# 5. Check for new commit
# ---------------------------------------------------------

echo "[Updater] Fetching repository..."
git fetch origin "$BRANCH"

LOCAL_COMMIT=$(git rev-parse HEAD)
REMOTE_COMMIT=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL_COMMIT" = "$REMOTE_COMMIT" ]; then
    echo "[Updater] Already running latest version."
    exit 0
fi

echo "[Updater] New version available."
echo "[Updater] Current: $LOCAL_COMMIT"
echo "[Updater] Latest:  $REMOTE_COMMIT"

# ---------------------------------------------------------
# 6. Deploy
# ---------------------------------------------------------

git pull --ff-only origin "$BRANCH"

if [ -f requirements.txt ]; then
    echo "[Updater] Updating Python dependencies..."
    python3 -m pip install --user -r requirements.txt
fi

echo "[Updater] Deployment completed."