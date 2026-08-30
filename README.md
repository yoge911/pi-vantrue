# pi-vantrue

A Raspberry Pi OS automation system for automatically backing up videos from a Vantrue E3 dashcam.

---

## Overview & Hardware Environment

- **Hardware**: Raspberry Pi 4 (Raspberry Pi OS)
- **User**: `picar` | **Hostname**: `vantrue-pi`
- **Repo Directory**: `/home/picar/vantrue-automation/pi-vantrue`
- **Wi-Fi Interface**: Single Wi-Fi adapter managed via `NetworkManager` / `nmcli`

---

## System Architecture & Separation of Responsibilities

```
CAR / PI GETS POWER
        |
        v
Raspberry Pi boots
        |
        v
vantrue-updater.service (scripts/update.sh)
        |
        +---> Try iPhone hotspot (max ~40s wall-clock deadline)
        |       |
        |       +-- Connected --> Check Git remote & pull updates
        |       |                 Sync changed systemd unit files
        |       |
        |       +-- Unavailable / Failure --> Log and exit cleanly (code 0)
        v
vantrue.service (main.py)
        |
        +---> Try Vantrue dashcam Wi-Fi
                |
                +-- Connected -----> Run Vantrue video synchronization
                |
                +-- Unavailable --> Wait (30s) and retry in main loop
```

### 1. Boot Updater (`vantrue-updater.service` & `scripts/update.sh`)

- **Boot-time update window**: Enforces a strict 40-second wall-clock deadline (`now - start_time`) to discover and connect to the iPhone hotspot.
- **Active Scanning & Bounded Calls**: Triggers `sudo -n nmcli device wifi rescan` and uses `timeout` guarded `sudo -n nmcli --wait <N> connection up "iPhone"` calls.
- **Non-blocking Execution**: On any error (hotspot unavailable, Git unreachable, local uncommitted changes, or pull failure), the updater logs the event and exits cleanly with code `0`.
- **Automated Service Sync**: Automatically compares `$APP_DIR/systemd/*.service` against `/etc/systemd/system/` and updates unit files followed by `sudo -n systemctl daemon-reload`.

### 2. Main Service (`vantrue.service` & `main.py`)

- **Decoupled Startup**: Configured with `Wants=vantrue-updater.service` so `main.py` runs regardless of whether the updater succeeded, timed out, or skipped updating.
- **Operational Focus**: Periodically attempts connection to `VANTRUE_NETWORK` using `sudo -n nmcli --wait 10`.
- **Fault-Tolerant Retry Loop**: If Vantrue Wi-Fi is unavailable, logs status and sleeps for `RETRY_CYCLE_SECONDS` (30s) before retrying without crashing.
- **Network Isolation**: Does not actively override or switch to Home Wi-Fi, leaving NetworkManager to handle ambient SSH/development connections when un-managed.

### 3. Preservation Request HTTP API (`preserve-api.service` & `preserve_api.py`)

- **iPhone Shortcut Endpoint**: Provides a lightweight HTTP server listening on `0.0.0.0:8765` for receiving ISO-8601 video preservation requests from iPhone Shortcuts.
- **Endpoints**:
  - `GET /health` ➔ `200 OK` (`{"status": "ok"}`)
  - `POST /preserve` ➔ Accepts JSON request body (`request_id`, `from`, `to`, `status="pending"`), validates ISO-8601 datetimes, and persists requests into SQLite `preservation_requests`.
- **Idempotency**: Duplicate `request_id` submissions return `200 OK` (`{"status": "already_exists", "request_id": "..."}`) without creating duplicate rows.
- **SQLite Concurrency**: Database operates in WAL mode (`PRAGMA journal_mode=WAL;` and `PRAGMA busy_timeout=5000;`) to safely handle concurrent access with `vantrue.service`.

---

## Repository Structure

```
pi-vantrue/
├── README.md                   # System overview and setup instructions
├── main.py                     # Main operational service logic
├── preserve_api.py             # Preservation Request HTTP API server
├── uploader.py                 # rclone Google Drive cloud uploader
├── vantrue_sync.py             # HTTP video discovery & download pipeline
├── db.py                       # SQLite SyncDB database manager
├── config.py                   # Central system configuration defaults
├── scripts/
│   └── update.sh               # Boot updater script
└── systemd/
    ├── vantrue-updater.service # Oneshot boot updater service
    ├── vantrue.service         # Main operational service
    └── preserve-api.service    # Preservation Request HTTP API service
```

---

## Installation & Deployment

### 1. Sudoers Configuration

To enable non-interactive Wi-Fi connection switching, systemd unit file synchronization, and daemon reloads, create `/etc/sudoers.d/picar`:

```bash
picar ALL=(ALL) NOPASSWD: /usr/bin/nmcli, /bin/cp /home/picar/vantrue-automation/pi-vantrue/systemd/*.service /etc/systemd/system/, /usr/bin/systemctl daemon-reload
```

Ensure correct permissions on the sudoers file:

```bash
sudo chmod 0440 /etc/sudoers.d/picar
```

### 2. Initial Systemd Service Registration

Copy service files into `/etc/systemd/system/`, reload the daemon, and enable all services:

```bash
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable vantrue-updater.service vantrue.service preserve-api.service
sudo systemctl start preserve-api.service
```

---

## Verification & Monitoring

To monitor updater, operational service, and preservation API logs on the Raspberry Pi:

```bash
# Check status of all services
sudo systemctl status vantrue-updater.service vantrue.service preserve-api.service

# Stream live journal logs
journalctl -u vantrue-updater.service -u vantrue.service -u preserve-api.service -f

# Inspect boot logs without pager
journalctl -u vantrue-updater.service -u vantrue.service -u preserve-api.service -b --no-pager
```

