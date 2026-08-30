import subprocess
import sys
import time


VANTRUE_NETWORK = "E3_VANTRUE_13c6"   # Replace with exact nmcli connection name
IPHONE_NETWORK = "iPhone"             # Target connection for future cloud uploads

RETRY_CYCLE_SECONDS = 30


def connect(network_name: str, timeout_seconds: int = 10) -> bool:
    """Try to activate a saved NetworkManager connection."""

    print(f"[Network] Trying: {network_name}", flush=True)

    try:
        result = subprocess.run(
            [
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
    Vantrue synchronization workflow.

    We will implement this next:
      1. Read Vantrue HTTP file listing
      2. Find unsynced videos
      3. Start with oldest files
      4. Download until local buffer limit is reached
      5. Store sync state
    """

    print("[Vantrue] Starting video sync...", flush=True)

    # Placeholder for now.

    print("[Vantrue] Video sync finished.", flush=True)


def network_cycle():
    """
    Main operational network logic.

    Separation of Responsibilities:
    - vantrue-updater.service handles boot-time iPhone hotspot connection for Git update.
    - main.py handles operational Vantrue Wi-Fi synchronization.
    - If Vantrue Wi-Fi is unavailable, wait and retry. Home Wi-Fi is left to NetworkManager
      for ambient SSH/development access and is not actively managed here.
    - Future cloud-upload flow: after buffering videos from Vantrue, main.py will switch
      to IPHONE_NETWORK to upload files to cloud, then return to VANTRUE_NETWORK.
    """

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