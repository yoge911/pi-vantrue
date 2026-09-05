import logging
import subprocess
import sys
import time
from typing import Optional

from config import Config
from logger import setup_logging
from uploader import VantrueUploader
from vantrue_sync import VantrueSyncEngine

from transfer_state import is_transfer_in_progress

RETRY_CYCLE_SECONDS = Config.HOTSPOT_RETRY_INTERVAL


def check_interface_exists(interface: str) -> bool:
    """Check if a network interface exists on the system."""
    try:
        res = subprocess.run(
            ["ip", "link", "show", interface],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return res.returncode == 0
    except Exception:
        return False


def is_ssid_visible(ssid_name: str, interface: str = "wlan0", rescan: bool = True) -> bool:
    """
    Check if a Wi-Fi SSID is currently visible in nearby scan results on the specified interface.
    Does NOT disrupt active connections on other interfaces.
    """
    logger = logging.getLogger(f"network.{interface}")
    if not check_interface_exists(interface):
        logger.debug(f"Interface '{interface}' is not present on system.")
        return False

    if rescan:
        try:
            subprocess.run(
                ["sudo", "-n", "nmcli", "device", "wifi", "rescan", "ifname", interface],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass

    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "SSID", "device", "wifi", "list", "ifname", interface],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            visible_ssids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            return ssid_name in visible_ssids
    except Exception as exc:
        logger.warning(f"Error scanning for SSID '{ssid_name}' on {interface}: {exc}")

    return False


def get_active_wifi_connection(interface: str) -> Optional[str]:
    """Return the name of the active connection on the specified interface, if any."""
    if not check_interface_exists(interface):
        return None
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "GENERAL.CONNECTION", "device", "show", interface],
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
        logging.getLogger(f"network.{interface}").warning(
            f"Error querying active connection on {interface}: {exc}"
        )

    return None


def connect(
    network_name: str,
    interface: str,
    timeout_seconds: int = 10,
    check_visibility: bool = True,
) -> bool:
    """
    Try to activate a saved NetworkManager connection on a specific interface.
    Explicitly targets the given interface (e.g. ifname wlan0 or ifname wlan1).
    """
    logger = logging.getLogger(f"network.{interface}")

    if not check_interface_exists(interface):
        logger.warning(f"Cannot connect to '{network_name}': interface {interface} is absent.")
        return False

    active_conn = get_active_wifi_connection(interface)
    if active_conn == network_name:
        logger.info(f"Already connected to '{network_name}' on interface {interface}.")
        return True

    if check_visibility and not is_ssid_visible(network_name, interface=interface, rescan=True):
        logger.info(
            f"SSID '{network_name}' is not visible on interface {interface}. Skipping connection attempt."
        )
        return False

    logger.info(f"Initiating connection to '{network_name}' on interface {interface}...")

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
                "ifname",
                interface,
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds + 2,
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"Connection attempt to '{network_name}' on {interface} timed out.")
        return False
    except Exception as exc:
        logger.error(f"Failed to execute nmcli for '{network_name}' on {interface}: {exc}")
        return False

    if result.returncode == 0:
        logger.info(f"Successfully connected to '{network_name}' on interface {interface}.")
        return True

    err_msg = result.stderr.strip() if result.stderr else f"Exit code {result.returncode}"
    logger.warning(f"Could not connect to '{network_name}' on interface {interface}: {err_msg}")
    return False


def ensure_internet_connection_wlan1(uploader: Optional[VantrueUploader] = None) -> bool:
    """
    Ensure wlan1 has a healthy internet connection, managing priority hotspot selection:
      1. Preferred: Config.PREFERRED_HOTSPOT_SSID ("Vantrue-iPhone-Hotspot")
      2. Fallback: Config.FALLBACK_HOTSPOT_SSID (regular iPhone e.g. "iPhone" or "iPhone 1")

    Retries every Config.HOTSPOT_RETRY_INTERVAL (15s) when internet is down.
    Never interrupts an active file transfer.
    """
    logger = logging.getLogger(f"network.{Config.INTERNET_INTERFACE}")
    uploader_instance = uploader or VantrueUploader()

    if not check_interface_exists(Config.INTERNET_INTERFACE):
        logger.warning(f"Internet interface '{Config.INTERNET_INTERFACE}' is absent on system.")
        return False

    logger.info("wlan1: checking internet connectivity")
    has_internet = uploader_instance.check_internet_connectivity()
    active_conn = get_active_wifi_connection(Config.INTERNET_INTERFACE)

    pref_ssid = Config.PREFERRED_HOTSPOT_SSID
    fallback_candidates = list(
        dict.fromkeys(
            [Config.FALLBACK_HOTSPOT_SSID]
            + getattr(Config, "FALLBACK_HOTSPOT_CANDIDATES", ["iPhone", "iPhone 1"])
        )
    )

    # CASE 1: Internet is working
    if has_internet:
        if active_conn == pref_ssid:
            logger.info(f"wlan1: internet connection established via {pref_ssid}")
            return True
        else:
            # Connected to fallback hotspot or ambient connection (e.g. "iPhone", "iPhone 1")
            if is_transfer_in_progress():
                logger.info("wlan1: active transfer in progress; skipping preferred hotspot check for now")
                return True

            logger.info(
                f"wlan1: connected via {active_conn or 'fallback'}; scanning for preferred hotspot {pref_ssid}"
            )
            if is_ssid_visible(pref_ssid, interface=Config.INTERNET_INTERFACE, rescan=True):
                logger.info(f"wlan1: preferred hotspot {pref_ssid} became available; switching from fallback")
                if connect(pref_ssid, interface=Config.INTERNET_INTERFACE, check_visibility=False):
                    if uploader_instance.check_internet_connectivity():
                        logger.info(f"wlan1: internet connection established via {pref_ssid}")
                        return True
                    else:
                        logger.warning(
                            f"wlan1: preferred hotspot connection failed (no internet access); reverting to fallback"
                        )
                        revert_target = active_conn if active_conn else fallback_candidates[0]
                        connect(revert_target, interface=Config.INTERNET_INTERFACE, check_visibility=False)
            logger.info(f"wlan1: internet connection established via {active_conn or 'existing connection'}")
            return True

    # CASE 2: Connection lost or internet check failed -> Start Hotspot Recovery
    logger.info("wlan1: connection lost, starting hotspot recovery")
    logger.info(f"wlan1: scanning for preferred hotspot {pref_ssid}")

    pref_visible = is_ssid_visible(pref_ssid, interface=Config.INTERNET_INTERFACE, rescan=True)

    if pref_visible:
        logger.info(f"wlan1: trying preferred hotspot {pref_ssid}")
        if connect(pref_ssid, interface=Config.INTERNET_INTERFACE, check_visibility=False):
            if uploader_instance.check_internet_connectivity():
                logger.info(f"wlan1: internet connection established via {pref_ssid}")
                return True
            else:
                logger.warning("wlan1: preferred hotspot connection failed")
        else:
            logger.warning("wlan1: preferred hotspot connection failed")
    else:
        logger.info("wlan1: preferred hotspot unavailable")

    for fb in fallback_candidates:
        if is_ssid_visible(fb, interface=Config.INTERNET_INTERFACE, rescan=False):
            logger.info(f"wlan1: trying fallback iPhone hotspot '{fb}'")
            if connect(fb, interface=Config.INTERNET_INTERFACE, check_visibility=False):
                if uploader_instance.check_internet_connectivity():
                    logger.info(f"wlan1: internet connection established via {fb}")
                    return True
                else:
                    logger.warning(f"wlan1: fallback hotspot '{fb}' connection failed")

    logger.info(f"wlan1: no hotspot connection available, retrying in {Config.HOTSPOT_RETRY_INTERVAL} seconds")
    return False


def run_vantrue_sync():
    """
    Vantrue synchronization workflow:
      1. Query Vantrue HTTP directory listing
      2. Store discovered files in SQLite database
      3. Process pending videos in newest-first chronological order
      4. Verify local buffer & free space safety limits
      5. Atomically download to .part file and rename to .mp4 upon completion
    """
    logger = logging.getLogger("vantrue")
    logger.info("Starting video sync...")

    try:
        engine = VantrueSyncEngine()
        engine.run_sync(on_file_downloaded=run_upload_cycle)
    except Exception as exc:
        logger.error(f"Error during video sync: {exc}", exc_info=True)

    logger.info("Video sync finished.")


def run_upload_cycle():
    """Run Google Drive cloud upload cycle for pending recordings on wlan1 / Internet."""
    logger = logging.getLogger("upload")
    logger.info("Starting cloud upload check...")
    try:
        uploader = VantrueUploader()

        def connect_internet_fn(net_name: str) -> bool:
            return ensure_internet_connection_wlan1(uploader=uploader)

        uploader.run_upload_cycle(
            connect_wifi_fn=connect_internet_fn,
            iphone_network_name=Config.PREFERRED_HOTSPOT_SSID,
        )
    except Exception as exc:
        logger.error(f"Error during upload cycle: {exc}", exc_info=True)

    logger.info("Cloud upload check finished.")


def network_cycle():
    """
    Main operational network logic (Dual-Interface Architecture).

    - wlan0: Dedicated Vantrue Dashcam Data Interface (E3_VANTRUE_13c6 -> 192.168.1.254)
    - wlan1: Dedicated Management / SSH / Internet Interface (iPhone Hotspot / Home Wi-Fi)

    Both paths operate independently without cross-interface disruption.
    """
    logger = logging.getLogger("vantrue")

    if Config.IS_EXPLICIT_BASE_URL:
        logger.info(
            "VANTRUE_BASE_URL explicitly configured; skipping Wi-Fi activation for mock testing."
        )
        run_vantrue_sync()
        run_upload_cycle()
        return

    # --- PATH 1: VANTRUE DASHCAM SYNC ON wlan0 ---
    vantrue_iface = Config.VANTRUE_INTERFACE
    vantrue_net = Config.VANTRUE_NETWORK

    wlan0_present = check_interface_exists(vantrue_iface)
    wlan0_active = get_active_wifi_connection(vantrue_iface) if wlan0_present else None

    if not wlan0_present:
        logger.warning(f"Vantrue Wi-Fi interface '{vantrue_iface}' is absent on system.")
    else:
        vantrue_visible = is_ssid_visible(vantrue_net, interface=vantrue_iface, rescan=True)
        if vantrue_visible:
            logger.info(f"Vantrue SSID '{vantrue_net}' detected on {vantrue_iface}.")
            if connect(vantrue_net, interface=vantrue_iface, check_visibility=False):
                run_vantrue_sync()
        else:
            logger.info(
                f"Vantrue Wi-Fi '{vantrue_net}' not visible on {vantrue_iface}. Active connection: {wlan0_active or 'none'}."
            )

    # --- PATH 2: CLOUD UPLOADS ON wlan1 / INTERNET ---
    uploader = VantrueUploader()
    pending_uploads = uploader.db.get_pending_uploads()

    if pending_uploads:
        logger.info(f"{len(pending_uploads)} recordings pending upload. Triggering upload check...")
        if ensure_internet_connection_wlan1(uploader=uploader):
            run_upload_cycle()


def main():
    setup_logging()
    logger = logging.getLogger("vantrue")
    logger.info("Vantrue Automation service started.")

    while True:
        try:
            network_cycle()
        except Exception as exc:
            logger.error(f"Unexpected error in main loop: {exc}", exc_info=True)

        logger.info(f"Next network check in {RETRY_CYCLE_SECONDS} seconds.")
        time.sleep(RETRY_CYCLE_SECONDS)


if __name__ == "__main__":
    main()