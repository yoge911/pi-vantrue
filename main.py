import subprocess
import sys
import time


VANTRUE_NETWORK = "E3_VANTRUE_13c6"   # Replace with exact nmcli connection name
HOME_NETWORK = "o2-WLAN96-2.4"

RETRY_CYCLE_SECONDS = 30


def connect(network_name: str) -> bool:
    """Try to activate a saved NetworkManager connection."""

    print(f"[Network] Trying: {network_name}", flush=True)

    result = subprocess.run(
        [
            "sudo",
            "-n",
            "nmcli",
            "connection",
            "up",
            network_name,
        ],
        capture_output=True,
        text=True,
    )

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

    Important:
    The iPhone/Git update step is NOT handled here.
    vantrue-updater.service already handles that during boot.
    """

    # Priority 1 after deployment: Vantrue dashcam
    if connect(VANTRUE_NETWORK):
        run_vantrue_sync()
        return

    # Vantrue unavailable -> try home network
    print(
        "[Network] Vantrue unavailable. Trying home Wi-Fi...",
        flush=True,
    )

    if connect(HOME_NETWORK):
        print(
            "[Network] Home Wi-Fi available. "
            "Waiting for next Vantrue attempt.",
            flush=True,
        )
        return

    # Nothing available
    print(
        "[Network] Neither Vantrue nor home Wi-Fi is available.",
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