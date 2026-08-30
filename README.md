# pi-vantrue

Raspberry Pi automation system for backing up videos from a Vantrue E3 dashcam.

## System Architecture

1. `vantrue-updater.service` (`scripts/update.sh`):
   - Runs at boot prior to `vantrue.service`.
   - Attempts iPhone hotspot connection for \~40s (wall-clock deadline).
   - If connected, checks Git origin, pulls changes (`git pull --ff-only`), and automatically syncs systemd unit files to `/etc/systemd/system/`.
   - Non-blocking: always exits cleanly with code `0` so main automation runs regardless of hotspot or Git availability.
2. `vantrue.service` (`main.py`):
   - Manages operational Wi-Fi connections (focuses on Vantrue dashcam Wi-Fi).
   - If Vantrue Wi-Fi is unavailable, waits and retries without exiting.
   - Home Wi-Fi is managed by NetworkManager for development SSH access and is not actively overridden by `main.py`.

## Systemd & Sudoers Setup

To enable non-interactive systemd unit file updates during Git deployment, add the following configuration to `/etc/sudoers.d/picar`:

```text
picar ALL=(ALL) NOPASSWD: /usr/bin/nmcli, /bin/cp /home/picar/vantrue-automation/pi-vantrue/systemd/*.service /etc/systemd/system/, /usr/bin/systemctl daemon-reload
```

### Initial Service Installation

```bash
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable vantrue-updater.service vantrue.service
```
