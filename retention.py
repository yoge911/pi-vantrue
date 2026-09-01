import logging
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config import Config
from db import SyncDB

logger = logging.getLogger("retention")
storage_logger = logging.getLogger("storage")
cleanup_logger = logging.getLogger("cleanup")


class RetentionManager:
    """
    Manages local rolling video cache retention and disk space safety limits.
    Enforces rules:
    1. Local files are retained after cloud upload as a rolling video cache.
    2. Deletion occurs ONLY when free disk space falls below MIN_FREE_SPACE_GB.
    3. ONLY confirmed uploaded files (status='uploaded') are eligible for deletion.
    4. Eligible files are deleted strictly from oldest to newest until free space is restored.
    5. Unsynced, pending, downloading, or uploading files are NEVER deleted.
    """

    def __init__(self, config: type = Config):
        self.config = config
        self.db = SyncDB(self.config.DB_PATH)

    def get_free_space_bytes(self) -> int:
        """Return available free disk space in bytes for the video buffer directory."""
        if not self.config.LOCAL_DOWNLOAD_DIR.exists():
            self.config.LOCAL_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(self.config.LOCAL_DOWNLOAD_DIR)
        return usage.free

    def cleanup_uploaded_files_if_needed(
        self, active_filenames: Optional[List[str]] = None
    ) -> Tuple[bool, str]:
        """
        Evaluate disk space and delete oldest uploaded files if free space < threshold.
        Returns (success_or_safe, status_code).
        """
        active_set = set(active_filenames) if active_filenames else set()
        threshold_bytes = self.config.MIN_FREE_DISK_BYTES
        threshold_gb = self.config.MIN_FREE_SPACE_GB

        free_bytes = self.get_free_space_bytes()
        free_gb = free_bytes / (1024 ** 3)

        if free_bytes >= threshold_bytes:
            storage_logger.info(
                f"Free space: {free_gb:.1f} GB; threshold: {threshold_gb} GB; no cleanup required"
            )
            return True, "ok"

        storage_logger.info(f"Free space: {free_gb:.1f} GB; threshold: {threshold_gb} GB")
        cleanup_logger.info("Storage cleanup started")

        uploaded_records = self.db.get_uploaded_recordings()

        if not uploaded_records:
            cleanup_logger.error(
                f"[CRITICAL] Disk space below safety threshold ({free_gb:.1f} GB free < {threshold_gb} GB limit) and no safely uploaded videos remain. Downloads paused to protect unsynced footage."
            )
            return False, "no_uploaded_files_remain"

        for rec in uploaded_records:
            filename = rec["filename"]
            remote_url = rec["remote_url"]
            local_path = self.config.LOCAL_DOWNLOAD_DIR / filename

            # Protect active files currently downloading/uploading/streaming
            if filename in active_set or f"{filename}.part" in active_set:
                cleanup_logger.debug(f"Skipping active file '{filename}' during cleanup.")
                continue

            # Reconcile state if local file missing on disk
            if not local_path.exists():
                cleanup_logger.info(
                    f"File '{filename}' marked as uploaded but missing from disk. Reconciling DB state."
                )
                self.db.mark_deleted(remote_url)
                continue

            try:
                file_bytes = local_path.stat().st_size
                cleanup_logger.info(f"Removing oldest uploaded video: {filename}")
                local_path.unlink()
                self.db.mark_deleted(remote_url)

                recovered_mb = file_bytes / (1024 * 1024)
                cleanup_logger.info(f"Recovered {recovered_mb:.1f} MB")

                free_bytes = self.get_free_space_bytes()
                new_free_gb = free_bytes / (1024 ** 3)
                cleanup_logger.info(f"Free space now {new_free_gb:.1f} GB")

                if free_bytes >= threshold_bytes:
                    cleanup_logger.info(
                        f"Free space restored to {new_free_gb:.1f} GB. Finished."
                    )
                    return True, "cleanup_restored"
            except Exception as exc:
                cleanup_logger.error(f"Error deleting uploaded file '{filename}': {exc}")

        # Final check after iterating all uploaded files
        free_bytes = self.get_free_space_bytes()
        final_free_gb = free_bytes / (1024 ** 3)

        if free_bytes >= threshold_bytes:
            cleanup_logger.info(f"Free space restored to {final_free_gb:.1f} GB. Finished.")
            return True, "cleanup_restored"

        cleanup_logger.error(
            f"[CRITICAL] Disk space below safety threshold ({final_free_gb:.1f} GB free < {threshold_gb} GB limit) and no safely uploaded videos remain. Downloads paused to protect unsynced footage."
        )
        return False, "no_uploaded_files_remain"
