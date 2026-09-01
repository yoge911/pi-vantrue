import subprocess
import sys
import time
from typing import Optional

from config import Config
from uploader import VantrueUploader
from vantrue_sync import VantrueSyncEngine


VANTRUE_NETWORK = "E3_VANTRUE_13c6"   # Exact nmcli connection name and SSID for Vantrue E3
IPHONE_NETWORK = "iPhone"             # Target connection for cloud uploads

RETRY_CYCLE_SECONDS = 30


def is_ssid_visible(ssid_name: str, rescan: bool = True) -> bool:
    """
    Check if a Wi-Fi SSID is currently visible in nearby scan results.
    Does NOT disrupt any active wlan0 connection.
    """
    if rescan:
        try:
            subprocess.run(
                ["sudo", "-n", "nmcli", "device", "wifi", "rescan"],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass

    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "SSID", "device", "wifi", "list"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            visible_ssids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            return ssid_name in visible_ssids
    except Exception as exc:
        print(f"[Network] Error scanning for SSID '{ssid_name}': {exc}", flush=True)

    return False


def get_active_wifi_connection() -> Optional[str]:
    """Return the name of the active connection on wlan0, if any."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "GENERAL.CONNECTION", "device", "show", "wlan0"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.split(":", 1)
                if len(parts) == 2 and parts[1].strip() and parts[1].strip() != "--":
                    return parts[1].strip()
    except Exception as exc:
        print(f"[Network] Error querying active connection on wlan0: {exc}", flush=True)

    return None


def connect(network_name: str, timeout_seconds: int = 10, check_visibility: bool = True) -> bool:
    """
    Try to activate a saved NetworkManager connection safely.
    Verifies SSID visibility first if check_visibility=True to avoid disrupting healthy connections.
    """
    active_conn = get_active_wifi_connection()
    if active_conn == network_name:
        print(f"[Network] Already connected to {network_name}", flush=True)
        return True

    if check_visibility and not is_ssid_visible(network_name, rescan=True):
        print(
            f"[Network] SSID '{network_name}' is not visible. Skipping connection attempt.",
            flush=True,
        )
        return False

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
        engine.run_sync(on_file_downloaded=run_upload_cycle)
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
            # Only return to Vantrue if Vantrue SSID is actually visible nearby
            if is_ssid_visible(VANTRUE_NETWORK, rescan=False):
                print("[Upload] Vantrue network detected nearby. Returning to Vantrue network...", flush=True)
                connect(VANTRUE_NETWORK, check_visibility=False)

    print("[Upload] Cloud upload check finished.", flush=True)


def network_cycle():
    """
    Main operational network logic.

    Separation of Responsibilities:
    - vantrue-updater.service handles boot-time iPhone hotspot connection for Git update.
    - main.py handles operational Vantrue Wi-Fi synchronization and rclone upload pipeline.
    - Non-disruptive probing: Checks SSID visibility before issuing any destructive `nmcli connection up`.
    - Mock/Development Mode: If VANTRUE_BASE_URL is explicitly set in environment,
      skip Wi-Fi activation and proceed directly to download and upload cycles.
    """

    if Config.IS_EXPLICIT_BASE_URL:
        print(
            "[Network] VANTRUE_BASE_URL explicitly configured; skipping Vantrue Wi-Fi activation for mock testing.",
            flush=True,
        )
        run_vantrue_sync()
        run_upload_cycle()
        return

    # Check active connection & visible SSIDs non-disruptively
    active_conn = get_active_wifi_connection()
    vantrue_visible = is_ssid_visible(VANTRUE_NETWORK, rescan=True)

    if vantrue_visible:
        print(f"[Network] Vantrue SSID '{VANTRUE_NETWORK}' detected nearby.", flush=True)
        if connect(VANTRUE_NETWORK, check_visibility=False):
            run_vantrue_sync()
            run_upload_cycle()
            return

    # Vantrue is not visible. If we have active Wi-Fi or pending uploads, check upload stage.
    print(
        f"[Network] Vantrue Wi-Fi '{VANTRUE_NETWORK}' not visible. Active wlan0: {active_conn or 'none'}.",
        flush=True,
    )

    uploader = VantrueUploader()
    pending_uploads = uploader.db.get_pending_uploads()

    if pending_uploads:
        print(f"[Network] {len(pending_uploads)} recordings pending upload. Checking internet connection...", flush=True)
        # If internet is already available (e.g. Home Wi-Fi or Ethernet), or if iPhone hotspot is visible, run upload cycle
        if uploader.check_internet_connectivity() or is_ssid_visible(IPHONE_NETWORK, rescan=False):
            run_upload_cycle()


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
            f"[Vantrue] Next network check in {RETRY_CYCLE_SECONDS} seconds.",
            flush=True,
        )

        time.sleep(RETRY_CYCLE_SECONDS)


if __name__ == "__main__":
    main()