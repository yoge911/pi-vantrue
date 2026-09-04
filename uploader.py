import logging
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional

from config import Config
from db import SyncDB
from transfer_state import set_transfer_in_progress

logger = logging.getLogger("upload")


class VantrueUploader:
    """Automated Google Drive uploader via rclone for buffered Vantrue recordings."""

    def __init__(self, config: type = Config):
        self.config = config
        self.db = SyncDB(self.config.DB_PATH)

    def check_internet_connectivity(self, timeout_seconds: int = 5) -> bool:
        """Perform a quick, bounded connectivity check to verify usable internet."""
        url = self.config.INTERNET_CHECK_URL
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "VantruePiAutomation/1.0"}
            )
            with urllib.request.urlopen(req, timeout=timeout_seconds) as response:
                if response.status in (200, 204):
                    logger.debug("Internet connectivity verified successfully.")
                    return True
                logger.warning(f"Internet check returned HTTP status {response.status}.")
                return False
        except Exception as exc:
            logger.info(f"Internet connectivity check unavailable: {exc}")
            return False

    def upload_file_rclone(self, recording: Dict) -> bool:
        """
        Upload a single file to Google Drive using rclone copyto.
        Returns True if rclone process exits cleanly with return code 0.
        """
        filename = recording["filename"]
        local_path = self.config.LOCAL_DOWNLOAD_DIR / filename

        if not local_path.exists() or not local_path.is_file():
            logger.warning(
                f"Local file '{filename}' does not exist. Skipping upload."
            )
            return False

        if filename.endswith(".part"):
            logger.warning(f"Skipping incomplete temporary file '{filename}'.")
            return False

        rclone_target = (
            f"{self.config.RCLONE_REMOTE}{self.config.RCLONE_DESTINATION}/{filename}"
        )
        file_size_bytes = local_path.stat().st_size if local_path.exists() else 0
        file_size_mb = file_size_bytes / (1024 * 1024)

        logger.info(f"Upload started file={filename} size={file_size_mb:.1f}MB target={rclone_target}")

        cmd = [
            "rclone",
            "copyto",
            str(local_path),
            rclone_target,
        ]

        start_time = time.time()
        set_transfer_in_progress(True)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.config.RCLONE_UPLOAD_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            duration = time.time() - start_time
            logger.error(
                f"rclone timed out uploading '{filename}' after {duration:.1f}s. Local copy preserved."
            )
            return False
        except Exception as exc:
            logger.error(
                f"Failed to execute rclone for '{filename}': {exc}. Local copy preserved."
            )
            return False
        finally:
            set_transfer_in_progress(False)

        duration = time.time() - start_time
        if result.returncode == 0:
            logger.info(
                f"Upload completed file={filename} bytes={file_size_bytes} duration={duration:.1f}s"
            )
            return True

        err_msg = result.stderr.strip() if result.stderr else f"Exit code {result.returncode}"
        logger.error(
            f"rclone failed for '{filename}': {err_msg}. Local copy preserved."
        )
        return False

    def run_upload_cycle(
        self,
        connect_wifi_fn: Optional[Callable[[str], bool]] = None,
        iphone_network_name: str = "iPhone 1",
    ):
        """
        Execute full upload cycle for pending recordings.
        Sequence:
          1. Query pending uploads from DB.
          2. Check network state (Mock mode vs Production mode).
          3. Upload one file at a time using rclone.
          4. Safe state transition: uploaded -> delete local file -> deleted.
        """
        pending = self.db.get_pending_uploads()

        if not pending:
            logger.debug("No recordings awaiting cloud upload.")
            return

        logger.info(f"{len(pending)} recordings awaiting upload.")

        # Network handling
        if self.config.IS_EXPLICIT_BASE_URL:
            logger.info("Mock mode active; verifying internet on current network...")
            if not self.check_internet_connectivity():
                logger.info("Internet unavailable on current network. Upload postponed.")
                return
        else:
            # Production mode: Check internet connectivity first (e.g. wlan1 already connected to iPhone or Home Wi-Fi)
            if not self.check_internet_connectivity():
                if connect_wifi_fn:
                    logger.info(f"Connecting wlan1 to hotspot network '{iphone_network_name}'...")
                    if not connect_wifi_fn(iphone_network_name):
                        logger.info("Hotspot network unavailable. Upload postponed.")
                        return

                if not self.check_internet_connectivity():
                    logger.info("Internet connection unavailable on hotspot. Upload postponed.")
                    return

        logger.info("Internet connection available for cloud upload.")

        for rec in pending:
            recording_dict = dict(rec)
            remote_url = recording_dict["remote_url"]
            filename = recording_dict["filename"]
            local_path = self.config.LOCAL_DOWNLOAD_DIR / filename

            # 1. Upload via rclone
            success = self.upload_file_rclone(recording_dict)

            if not success:
                logger.warning(f"Upload failed for '{filename}'. Halting upload cycle.")
                break

            # 2. Confirmed upload -> Mark uploaded in SQLite & retain local copy for rolling cache
            self.db.mark_uploaded(remote_url)
            logger.info(f"Upload verified for '{filename}'. Retaining file in local rolling cache.")



