import subprocess
import sys
import time

from config import Config
from uploader import VantrueUploader
from vantrue_sync import VantrueSyncEngine


VANTRUE_NETWORK = "E3_VANTRUE_13c6"   # Replace with exact nmcli connection name
IPHONE_NETWORK = "iPhone"             # Target connection for cloud uploads

RETRY_CYCLE_SECONDS = 30


def connect(network_name: str, timeout_seconds: int = 10) -> bool:
    """Try to activate a saved NetworkManager connection."""

    print(f"[Network] Trying: {network_name}", flush=True)

    try:
        result = subprocess.run(
            [
                "sudo",
                "-n",
                "nmcli",
                "--wait",
                str(timeout_seconds),
                "connection",
                "up",
                network_name,
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds + 2,
        )
    except subprocess.TimeoutExpired:
        print(f"[Network] Connection attempt to {network_name} timed out.", flush=True)
        return False
    except Exception as exc:
        print(f"[Network] Failed to execute nmcli for {network_name}: {exc}", flush=True)
        return False

    if result.returncode == 0:
        print(f"[Network] Connected to {network_name}", flush=True)
        return True

    print(f"[Network] Could not connect to {network_name}", flush=True)

    if result.stderr:
        print(f"[Network] {result.stderr.strip()}", flush=True)

    return False


def run_vantrue_sync():
    """
    Vantrue synchronization workflow:
      1. Query Vantrue HTTP directory listing
      2. Store discovered files in SQLite database
      3. Process pending videos in oldest-first chronological order
      4. Verify local buffer & free space safety limits
      5. Atomically download to .part file and rename to .mp4 upon completion
    """

    print("[Vantrue] Starting video sync...", flush=True)

    try:
        engine = VantrueSyncEngine()
        engine.run_sync()
    except Exception as exc:
        print(f"[Vantrue] Error during video sync: {exc}", flush=True)

    print("[Vantrue] Video sync finished.", flush=True)


def run_upload_cycle():
    """Run Google Drive cloud upload cycle for pending recordings."""
    print("[Upload] Starting cloud upload check...", flush=True)
    try:
        uploader = VantrueUploader()
        uploader.run_upload_cycle(
            connect_wifi_fn=connect,
            iphone_network_name=IPHONE_NETWORK,
        )
    except Exception as exc:
        print(f"[Upload] Error during upload cycle: {exc}", flush=True)
    finally:
        if not Config.IS_EXPLICIT_BASE_URL:
            print("[Upload] Returning to Vantrue network...", flush=True)
            connect(VANTRUE_NETWORK)

    print("[Upload] Cloud upload check finished.", flush=True)


def network_cycle():
    """
    Main operational network logic.

    Separation of Responsibilities:
    - vantrue-updater.service handles boot-time iPhone hotspot connection for Git update.
    - main.py handles operational Vantrue Wi-Fi synchronization and rclone upload pipeline.
    - Mock/Development Mode: If VANTRUE_BASE_URL is explicitly set in environment,
      skip Wi-Fi activation and proceed directly to download and upload cycles.
    - Production Mode: Connect to VANTRUE_NETWORK -> run download sync -> switch to
      iPhone hotspot -> upload pending files to Google Drive -> return to VANTRUE_NETWORK.
    """

    if Config.IS_EXPLICIT_BASE_URL:
        print(
            "[Network] VANTRUE_BASE_URL explicitly configured; skipping Vantrue Wi-Fi activation for mock testing.",
            flush=True,
        )
        run_vantrue_sync()
        run_upload_cycle()
        return

    if connect(VANTRUE_NETWORK):
        run_vantrue_sync()
        run_upload_cycle()
        return

    print(
        "[Network] Vantrue Wi-Fi unavailable. Will retry in next cycle.",
        flush=True,
    )


def main():
    print(
        "[Vantrue] Automation service started.",
        flush=True,
    )

    while True:
        try:
            network_cycle()

        except Exception as exc:
            # We do not want an unexpected error to permanently kill
            # the car automation.
            print(
                f"[Vantrue] Unexpected error: {exc}",
                flush=True,
            )

        print(
            f"[Vantrue] Next network check in "
            f"{RETRY_CYCLE_SECONDS} seconds.",
            flush=True,
        )

        time.sleep(RETRY_CYCLE_SECONDS)


if __name__ == "__main__":
    main()