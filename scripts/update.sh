#!/bin/bash

set -e

APP_DIR="/home/picar/vantrue-automation/pi-vantrue"
BRANCH="main"
IPHONE_CONNECTION="iPhone"

MAX_WAIT_SECONDS=40
RETRY_INTERVAL=3
DEFAULT_NMCLI_TIMEOUT=4

echo "[Updater] Starting boot update process..."
echo "[Updater] Waiting for iPhone hotspot..."

start_time=$(date +%s)
iphone_connected=false

# Trigger an initial active Wi-Fi scan to discover nearby iPhone hotspot
sudo -n nmcli device wifi rescan >/dev/null 2>&1 || true

while true; do
    now=$(date +%s)
    elapsed=$((now - start_time))
    remaining=$((MAX_WAIT_SECONDS - elapsed))

    if [ "$remaining" -le 0 ]; then
        break
    fi

    # Calculate nmcli timeout so final attempt does not exceed 40s total wall-clock window
    if [ "$remaining" -lt "$DEFAULT_NMCLI_TIMEOUT" ]; then
        nmcli_wait="$remaining"
    else
        nmcli_wait="$DEFAULT_NMCLI_TIMEOUT"
    fi

    # Safe GNU timeout wrapper (nmcli wait + 1s cap, bounded by remaining time)
    timeout_cap=$((nmcli_wait + 1))
    if [ "$timeout_cap" -gt "$remaining" ]; then
        timeout_cap="$remaining"
    fi

    if [ "$timeout_cap" -ge 1 ]; then
        if timeout "$timeout_cap" sudo -n nmcli --wait "$nmcli_wait" connection up "$IPHONE_CONNECTION" >/dev/null 2>&1; then
            echo "[Updater] Connected to iPhone hotspot."
            iphone_connected=true
            break
        fi
    else
        if sudo -n nmcli --wait "$nmcli_wait" connection up "$IPHONE_CONNECTION" >/dev/null 2>&1; then
            echo "[Updater] Connected to iPhone hotspot."
            iphone_connected=true
            break
        fi
    fi

    echo "[Updater] iPhone hotspot not available yet. Retrying..."

    now=$(date +%s)
    elapsed=$((now - start_time))
    remaining=$((MAX_WAIT_SECONDS - elapsed))

    if [ "$remaining" -le 0 ]; then
        break
    fi

    # Trigger Wi-Fi rescan for next attempt
    sudo -n nmcli device wifi rescan >/dev/null 2>&1 || true

    if [ "$remaining" -lt "$RETRY_INTERVAL" ]; then
        sleep "$remaining"
    else
        sleep "$RETRY_INTERVAL"
    fi
done

if [ "$iphone_connected" = false ]; then
    echo "[Updater] iPhone hotspot unavailable after ${MAX_WAIT_SECONDS}s wall-clock timeout."
    echo "[Updater] Skipping repository update."
    exit 0
fi

sync_systemd_services() {
    local force_reload="${1:-false}"
    echo "[Updater] Checking systemd service unit files..."
    local updated=false

    for service_file in "$APP_DIR"/systemd/*.service; do
        [ -f "$service_file" ] || continue
        local service_name
        service_name=$(basename "$service_file")
        local target_path="/etc/systemd/system/$service_name"

        if [ ! -f "$target_path" ] || ! cmp -s "$service_file" "$target_path"; then
            echo "[Updater] Syncing $service_name to $target_path..."
            if sudo -n cp "$service_file" "$target_path" 2>/dev/null; then
                updated=true
            else
                echo "[Updater] Notice: Could not copy $service_name to /etc/systemd/system/ (sudo privilege required)."
            fi
        fi
    done

    if [ "$updated" = true ] || [ "$force_reload" = true ]; then
        echo "[Updater] Reloading systemd manager configuration..."
        if sudo -n systemctl daemon-reload 2>/dev/null; then
            echo "[Updater] systemd daemon reloaded successfully."
        else
            echo "[Updater] Notice: systemctl daemon-reload failed (sudo privilege required)."
        fi
    fi
}

echo "[Updater] Checking for systemd service changes..."
sync_systemd_services false

cd "$APP_DIR" || {
    echo "[Updater] Could not change directory to $APP_DIR."
    echo "[Updater] Skipping update."
    exit 0
}

echo "[Updater] Checking internet/Git repository..."

if ! git ls-remote origin >/dev/null 2>&1; then
    echo "[Updater] Git repository is unreachable or authentication failed."
    echo "[Updater] Skipping update."
    exit 0
fi

if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    echo "[Updater] Local repository contains uncommitted changes."
    echo "[Updater] Skipping update to preserve local state."
    exit 0
fi

echo "[Updater] Fetching repository..."

if ! git fetch origin "$BRANCH"; then
    echo "[Updater] Git fetch failed."
    echo "[Updater] Skipping update."
    exit 0
fi

LOCAL_COMMIT=$(git rev-parse HEAD 2>/dev/null || echo "unknown_local")
REMOTE_COMMIT=$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo "unknown_remote")

echo "[Updater] Local commit:  $LOCAL_COMMIT"
echo "[Updater] Remote commit: $REMOTE_COMMIT"

if [ "$LOCAL_COMMIT" = "$REMOTE_COMMIT" ]; then
    echo "[Updater] Already running latest version."
    exit 0
fi

echo "[Updater] New version available."
echo "[Updater] Pulling update..."

CHANGED_FILES=$(git diff --name-only "$LOCAL_COMMIT" "$REMOTE_COMMIT" 2>/dev/null || true)

if ! git pull --ff-only origin "$BRANCH"; then
    echo "[Updater] Git pull failed."
    echo "[Updater] Skipping update."
    exit 0
fi

echo "[Updater] Repository updated successfully."

SYSTEMD_CHANGED=false
if echo "$CHANGED_FILES" | grep -q '^systemd/'; then
    SYSTEMD_CHANGED=true
fi

sync_systemd_services "$SYSTEMD_CHANGED"

if [ -f requirements.txt ]; then
    echo "[Updater] Updating Python dependencies..."
    if ! python3 -m pip install --user -r requirements.txt; then
        echo "[Updater] Python dependency update failed, but repository update succeeded."
    fi
fi

echo "[Updater] Deployment completed."
exit 0