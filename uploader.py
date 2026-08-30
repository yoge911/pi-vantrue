import os
import subprocess
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional

from config import Config
from db import SyncDB


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
                return response.status in (200, 204)
        except Exception as exc:
            print(f"[Upload] Internet connectivity check failed: {exc}", flush=True)
            return False

    def upload_file_rclone(self, recording: Dict) -> bool:
        """
        Upload a single file to Google Drive using rclone copyto.
        Returns True if rclone process exits cleanly with return code 0.
        """
        filename = recording["filename"]
        local_path = self.config.LOCAL_DOWNLOAD_DIR / filename

        if not local_path.exists() or not local_path.is_file():
            print(
                f"[Upload] Warning: Local file {filename} does not exist. Skipping upload.",
                flush=True,
            )
            return False

        if filename.endswith(".part"):
            print(
                f"[Upload] Skipping incomplete temporary file {filename}.",
                flush=True,
            )
            return False

        rclone_target = (
            f"{self.config.RCLONE_REMOTE}{self.config.RCLONE_DESTINATION}/{filename}"
        )
        print(f"[Upload] Uploading {filename} to {rclone_target}...", flush=True)

        cmd = [
            "rclone",
            "copyto",
            str(local_path),
            rclone_target,
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.config.RCLONE_UPLOAD_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            print(
                f"[Upload] rclone timed out uploading {filename}. Local copy preserved.",
                flush=True,
            )
            return False
        except Exception as exc:
            print(
                f"[Upload] Failed to execute rclone for {filename}: {exc}. Local copy preserved.",
                flush=True,
            )
            return False

        if result.returncode == 0:
            print(f"[Upload] Upload completed: {filename}", flush=True)
            return True

        err_msg = result.stderr.strip() if result.stderr else f"Exit code {result.returncode}"
        print(
            f"[Upload] rclone failed for {filename}: {err_msg}. Local copy preserved.",
            flush=True,
        )
        return False

    def run_upload_cycle(self, connect_wifi_fn: Optional[Callable[[str], bool]] = None, iphone_network_name: str = "iPhone"):
        """
        Execute full upload cycle for pending recordings.
        Sequence:
          1. Query pending uploads from DB.
          2. Check network state (Mock mode vs Production mode).
          3. Upload one file at a time using rclone.
          4. Safe state transition: uploaded -> delete local file -> deleted.
          5. Return to Vantrue network if switched.
        """
        pending = self.db.get_pending_uploads()

        if not pending:
            print("[Upload] No recordings awaiting cloud upload.", flush=True)
            return

        print(f"[Upload] {len(pending)} recordings awaiting upload.", flush=True)

        # Network handling
        if self.config.IS_EXPLICIT_BASE_URL:
            print(
                "[Upload] Mock mode active; verifying internet on current network...",
                flush=True,
            )
            if not self.check_internet_connectivity():
                print(
                    "[Upload] Internet unavailable on current network. Upload postponed.",
                    flush=True,
                )
                return
        else:
            # Production mode: Connect to iPhone hotspot using existing network function
            if connect_wifi_fn:
                print("[Upload] Connecting to iPhone hotspot...", flush=True)
                if not connect_wifi_fn(iphone_network_name):
                    print(
                        "[Upload] iPhone hotspot unavailable. Upload postponed.",
                        flush=True,
                    )
                    return

            if not self.check_internet_connectivity():
                print(
                    "[Upload] Internet connection unavailable on iPhone hotspot. Upload postponed.",
                    flush=True,
                )
                return

        print("[Upload] Internet connection available.", flush=True)

        for rec in pending:
            recording_dict = dict(rec)
            remote_url = recording_dict["remote_url"]
            filename = recording_dict["filename"]
            local_path = self.config.LOCAL_DOWNLOAD_DIR / filename

            # 1. Upload via rclone
            success = self.upload_file_rclone(recording_dict)

            if not success:
                print(
                    f"[Upload] Upload failed for {filename}. Halting upload cycle.",
                    flush=True,
                )
                break

            # 2. Confirmed upload -> Mark uploaded in SQLite
            self.db.mark_uploaded(remote_url)
            print(f"[Upload] Marked recording as uploaded: {filename}", flush=True)

            # 3. Local deletion -> Delete local video file
            try:
                if local_path.exists():
                    local_path.unlink()
                    print(f"[Upload] Deleted local copy: {filename}", flush=True)
            except Exception as exc:
                print(
                    f"[Upload] Warning: Local deletion failed for {filename}: {exc}",
                    flush=True,
                )

            # 4. Record local cleanup in SQLite
            self.db.mark_deleted(remote_url)
            print(f"[Upload] Local buffer space released for {filename}.", flush=True)
