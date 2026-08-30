import subprocess
import sys
import time

from config import Config
from vantrue_sync import VantrueSyncEngine


VANTRUE_NETWORK = "E3_VANTRUE_13c6"   # Replace with exact nmcli connection name
IPHONE_NETWORK = "iPhone"             # Target connection for future cloud uploads

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


def network_cycle():
    """
    Main operational network logic.

    Separation of Responsibilities:
    - vantrue-updater.service handles boot-time iPhone hotspot connection for Git update.
    - main.py handles operational Vantrue Wi-Fi synchronization.
    - Mock/Development Mode: If VANTRUE_BASE_URL is explicitly set in environment,
      skip Wi-Fi activation and proceed directly to run_vantrue_sync().
    - Production Mode: Connect to VANTRUE_NETWORK, then run_vantrue_sync().
      If Vantrue Wi-Fi is unavailable, wait and retry in next cycle.
    """

    if Config.IS_EXPLICIT_BASE_URL:
        print(
            "[Network] VANTRUE_BASE_URL explicitly configured; skipping Vantrue Wi-Fi activation for mock testing.",
            flush=True,
        )
        run_vantrue_sync()
        return

    if connect(VANTRUE_NETWORK):
        run_vantrue_sync()
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