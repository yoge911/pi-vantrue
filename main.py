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
    State-based hotspot connection management for wlan1:
      - STATE 1: Connected to Preferred Hotspot ("Vantrue-iPhone-Hotspot") & Internet Healthy
                 -> Maintain current connection quietly; DO NOT run active Wi-Fi scans.
      - STATE 2: Connected to Fallback Hotspot ("iPhone", "iPhone 1") & Internet Healthy
                 -> Check if active transfer is in progress (defer if busy).
                 -> If idle, scan for preferred hotspot and switch as soon as it is visible.
      - STATE 3: Connection Lost or No Internet
                 -> Actively scan wlan1 every 15s. Try preferred hotspot first, then fallback.
    """
    logger = logging.getLogger(f"network.{Config.INTERNET_INTERFACE}")
    uploader_instance = uploader or VantrueUploader()

    if not check_interface_exists(Config.INTERNET_INTERFACE):
        logger.warning(f"Internet interface '{Config.INTERNET_INTERFACE}' is absent on system.")
        return False

    has_internet = uploader_instance.check_internet_connectivity()
    active_conn = get_active_wifi_connection(Config.INTERNET_INTERFACE)

    pref_ssid = Config.PREFERRED_HOTSPOT_SSID
    fallback_candidates = list(
        dict.fromkeys(
            [Config.FALLBACK_HOTSPOT_SSID]
            + getattr(Config, "FALLBACK_HOTSPOT_CANDIDATES", ["iPhone", "iPhone 1"])
        )
    )

    # --- STATE 1: Connected to Preferred Hotspot & Internet Healthy ---
    if has_internet and active_conn == pref_ssid:
        logger.debug(f"wlan1: connected and internet healthy via preferred hotspot '{pref_ssid}'")
        return True

    # --- STATE 2: Connected to Fallback Hotspot / Ambient Connection & Internet Healthy ---
    if has_internet:
        if is_transfer_in_progress():
            logger.info("wlan1: active transfer in progress; deferring preferred hotspot scan")
            return True

        logger.info(
            f"wlan1: connected via fallback ('{active_conn or 'fallback'}'); scanning for preferred hotspot '{pref_ssid}'"
        )
        if is_ssid_visible(pref_ssid, interface=Config.INTERNET_INTERFACE, rescan=True):
            logger.info(f"wlan1: preferred hotspot '{pref_ssid}' detected; switching from fallback")
            if connect(pref_ssid, interface=Config.INTERNET_INTERFACE, check_visibility=False):
                if uploader_instance.check_internet_connectivity():
                    logger.info(f"wlan1: successfully switched to preferred hotspot '{pref_ssid}'")
                    return True
                else:
                    logger.warning(
                        f"wlan1: preferred hotspot connection failed (no internet); reverting to fallback '{active_conn}'"
                    )
                    revert_target = active_conn if active_conn else fallback_candidates[0]
                    connect(revert_target, interface=Config.INTERNET_INTERFACE, check_visibility=False)
            else:
                logger.warning(f"wlan1: preferred hotspot connection attempt failed")
        else:
            logger.info(
                f"wlan1: preferred hotspot '{pref_ssid}' unavailable; maintaining fallback connection ('{active_conn or 'fallback'}')"
            )
        return True

    # --- STATE 3: No Internet or Connection Lost (Hotspot Recovery Mode) ---
    logger.info("wlan1: internet connection down, starting hotspot recovery")
    logger.info(f"wlan1: scanning for preferred hotspot '{pref_ssid}'")

    if is_ssid_visible(pref_ssid, interface=Config.INTERNET_INTERFACE, rescan=True):
        logger.info(f"wlan1: trying preferred hotspot '{pref_ssid}'")
        if connect(pref_ssid, interface=Config.INTERNET_INTERFACE, check_visibility=False):
            if uploader_instance.check_internet_connectivity():
                logger.info(f"wlan1: internet connection established via preferred hotspot '{pref_ssid}'")
                return True
            else:
                logger.warning(f"wlan1: preferred hotspot '{pref_ssid}' connected but internet check failed")
        else:
            logger.warning(f"wlan1: preferred hotspot '{pref_ssid}' connection failed")
    else:
        logger.info(f"wlan1: preferred hotspot '{pref_ssid}' unavailable")

    for fb in fallback_candidates:
        if is_ssid_visible(fb, interface=Config.INTERNET_INTERFACE, rescan=False):
            logger.info(f"wlan1: trying fallback iPhone hotspot '{fb}'")
            if connect(fb, interface=Config.INTERNET_INTERFACE, check_visibility=False):
                if uploader_instance.check_internet_connectivity():
                    logger.info(f"wlan1: internet connection established via fallback '{fb}'")
                    return True
                else:
                    logger.warning(f"wlan1: fallback hotspot '{fb}' connected but internet check failed")

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

    # --- PATH 2: CLOUD UPLOADS & INTERNET MANAGEMENT ON wlan1 ---
    uploader = VantrueUploader()
    internet_active = ensure_internet_connection_wlan1(uploader=uploader)

    if internet_active:
        uploader.backfill_missing_drive_ids(max_batch=5)

    pending_uploads = uploader.db.get_pending_uploads()
    if pending_uploads:
        logger.info(f"{len(pending_uploads)} recordings pending upload. Triggering upload check...")
        if internet_active:
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