import html.parser
import logging
import os
import re
import shutil
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from config import Config
from db import SyncDB
from retention import RetentionManager

logger = logging.getLogger("sync")
storage_logger = logging.getLogger("storage")



class VantrueHTMLParser(html.parser.HTMLParser):
    """HTML parser to extract video file hyperlinks from HTTP directory listings."""

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.links: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]):
        if tag.lower() == "a":
            for name, val in attrs:
                if name.lower() == "href" and val:
                    # Resolve relative link against base_url
                    full_url = urllib.parse.urljoin(self.base_url, val)
                    unquoted_path = urllib.parse.unquote(full_url)
                    path_lower = unquoted_path.lower()

                    if any(path_lower.endswith(ext) for ext in Config.SUPPORTED_EXTENSIONS):
                        if full_url not in self.links:
                            self.links.append(full_url)


def extract_timestamp_from_filename(filename: str) -> str:
    """
    Extract a sortable timestamp string (YYYY-MM-DD HH:MM:SS) from a video filename.
    Handles Vantrue dashcam filename structures like:
      - 20260822_142136_00358_N_A.MP4
      - 20260830_105041_0001_A.MP4
      - 20260830105041.MP4
    """
    clean_name = Path(filename).name

    # Vantrue pattern: YYYYMMDD_HHMMSS (with optional sequence number e.g. 00358)
    match = re.search(r"(\d{4})[_-]?(\d{2})[_-]?(\d{2})[_-]?(\d{2})[_-]?(\d{2})[_-]?(\d{2})(?:[_-](\d+))?", clean_name)
    if match:
        year, month, day, hour, minute, second, seq = match.groups()
        formatted_time = f"{year}-{month}-{day} {hour}:{minute}:{second}"
        if seq:
            return f"{formatted_time}_{seq.zfill(5)}"
        return formatted_time

    # Secondary pattern: 8 digits (YYYYMMDD)
    match_date = re.search(r"(\d{4})[_-]?(\d{2})[_-]?(\d{2})", clean_name)
    if match_date:
        year, month, day = match_date.groups()
        return f"{year}-{month}-{day} 00:00:00"

    # Default fallback: return clean filename for lexicographical sorting
    return clean_name


class VantrueSyncEngine:
    """Main synchronization engine for Vantrue HTTP video downloads."""

    def __init__(self, config: type = Config):
        self.config = config
        self.db = SyncDB(self.config.DB_PATH)
        self.config.LOCAL_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    def scan_remote_recordings(self) -> List[Dict]:
        """Query HTTP directory listing and return discovered video metadata."""
        url = self.config.VANTRUE_BASE_URL
        logger.info(f"Querying Vantrue recordings directory from endpoint {url}...")

        req = urllib.request.Request(url, headers={"User-Agent": "VantruePiAutomation/1.0"})
        with urllib.request.urlopen(req, timeout=self.config.HTTP_TIMEOUT) as response:
            content_type = response.headers.get("Content-Type", "")
            html_content = response.read().decode("utf-8", errors="ignore")

        parser = VantrueHTMLParser(url)
        parser.feed(html_content)

        discovered = []
        for file_url in parser.links:
            filename = urllib.parse.unquote(Path(urllib.parse.urlparse(file_url).path).name)
            timestamp = extract_timestamp_from_filename(filename)

            # Try HEAD request to get file size if server supports it
            file_size = 0
            try:
                head_req = urllib.request.Request(file_url, method="HEAD", headers={"User-Agent": "VantruePiAutomation/1.0"})
                with urllib.request.urlopen(head_req, timeout=self.config.HTTP_TIMEOUT) as head_resp:
                    content_length = head_resp.headers.get("Content-Length")
                    if content_length and content_length.isdigit():
                        file_size = int(content_length)
            except Exception:
                pass  # Optional enhancement; ignore HEAD errors

            discovered.append({
                "remote_url": file_url,
                "filename": filename,
                "file_size": file_size,
                "recording_timestamp": timestamp,
            })

        logger.info(f"Discovered {len(discovered)} video files on dashcam HTTP server.")
        return discovered

    def get_current_local_buffer_size(self) -> int:
        """Calculate current total size of downloaded files in local buffer directory."""
        if not self.config.LOCAL_DOWNLOAD_DIR.exists():
            return 0
        total_size = 0
        for entry in self.config.LOCAL_DOWNLOAD_DIR.iterdir():
            if entry.is_file() and not entry.name.endswith(".part"):
                total_size += entry.stat().st_size
        return total_size

    def check_storage_limits(self, next_file_size: int = 0) -> Tuple[bool, str]:
        """
        Verify if downloading the next file satisfies:
        1. Local video buffer limit (MAX_BUFFER_BYTES)
        2. Minimum free disk space safety threshold (MIN_FREE_DISK_BYTES / MIN_FREE_SPACE_GB)
           by triggering rolling cleanup of oldest uploaded files if necessary.
        """
        current_buffer = self.get_current_local_buffer_size()
        max_buffer = self.config.MAX_BUFFER_BYTES

        if current_buffer + next_file_size > max_buffer:
            curr_gb = current_buffer / (1024 ** 3)
            max_gb = max_buffer / (1024 ** 3)
            storage_logger.warning(
                f"Local buffer safety ceiling reached ({curr_gb:.2f} GB / {max_gb:.2f} GB). Pausing download until space is released by cloud upload."
            )
            return False, "buffer_limit_reached"

        # Trigger rolling cache cleanup of oldest uploaded files if free space is below threshold
        retention_mgr = RetentionManager(self.config)
        safe, status_code = retention_mgr.cleanup_uploaded_files_if_needed()

        if not safe:
            return False, status_code

        # Re-verify actual disk space with next_file_size reserve
        free_space = retention_mgr.get_free_space_bytes()
        min_reserve = self.config.MIN_FREE_DISK_BYTES

        if free_space - next_file_size < min_reserve:
            free_gb = free_space / (1024 ** 3)
            reserve_gb = min_reserve / (1024 ** 3)
            storage_logger.warning(
                f"Disk space safety reserve limit reached. Available: {free_gb:.2f} GB (Required reserve: {reserve_gb:.2f} GB). Stopping download."
            )
            return False, "disk_reserve_reached"

        return True, "ok"


    def download_file(self, recording: Dict) -> bool:
        """
        Download a single video file using a temporary .part file and atomic rename.
        Restart-safe & idempotent.
        """
        remote_url = recording["remote_url"]
        filename = recording["filename"]

        final_path = self.config.LOCAL_DOWNLOAD_DIR / filename
        part_path = self.config.LOCAL_DOWNLOAD_DIR / f"{filename}.part"

        # If already downloaded physically and in DB, skip
        if final_path.exists() and final_path.stat().st_size > 0:
            if self.db.is_already_downloaded_or_synced(remote_url):
                logger.debug(f"File '{filename}' already downloaded and indexed in DB. Skipping.")
                return True

        # Remove incomplete .part file from previous interrupted run
        if part_path.exists():
            logger.info(f"Removing incomplete temporary file '{part_path.name}'.")
            part_path.unlink(missing_ok=True)

        logger.info(f"Download started file={filename} url={remote_url} dest={final_path}")
        start_time = time.time()

        req = urllib.request.Request(remote_url, headers={"User-Agent": "VantruePiAutomation/1.0"})

        try:
            with urllib.request.urlopen(req, timeout=self.config.HTTP_TIMEOUT) as response:
                if response.status != 200:
                    logger.error(f"HTTP error {response.status} downloading '{filename}'.")
                    return False

                total_size = int(response.headers.get("Content-Length", 0))

                with open(part_path, "wb") as out_file:
                    downloaded_bytes = 0
                    while True:
                        chunk = response.read(self.config.DOWNLOAD_CHUNK_SIZE)
                        if not chunk:
                            break
                        out_file.write(chunk)
                        downloaded_bytes += len(chunk)

            # Atomic rename from .part to final filename
            os.replace(part_path, final_path)

            duration = time.time() - start_time
            final_size = final_path.stat().st_size
            self.db.mark_downloaded(remote_url, final_size)

            size_mb = final_size / (1024 * 1024)
            logger.info(
                f"Download completed file={filename} bytes={final_size} duration={duration:.1f}s ({size_mb:.1f} MB)"
            )
            return True

        except Exception as exc:
            duration = time.time() - start_time
            logger.error(f"Download failed for '{filename}' after {duration:.1f}s: {exc}")
            if part_path.exists():
                part_path.unlink(missing_ok=True)
            return False

    def run_sync(self, on_file_downloaded: Optional[Callable[[], None]] = None):
        """Execute full video discovery, ordering, limit verification, and download loop."""
        try:
            discovered = self.scan_remote_recordings()
        except Exception as exc:
            logger.info(f"Vantrue HTTP endpoint unreachable: {exc}. Will retry in next cycle.")
            return

        if not discovered:
            logger.info("No video files found on Vantrue HTTP server.")
            return

        # Register in database
        self.db.register_recordings(discovered)

        # Get pending downloads sorted chronologically oldest-first
        pending = self.db.get_pending_downloads()

        if not pending:
            logger.info("All discovered videos have already been downloaded.")
            return

        logger.info(f"Found {len(pending)} pending videos to download from dashcam.")

        oldest_pending = pending[0]
        logger.info(f"Oldest pending recording: {oldest_pending['filename']} (Timestamp: {oldest_pending['recording_timestamp']})")

        for rec in pending:
            recording_dict = dict(rec)
            expected_size = recording_dict.get("file_size", 0)

            # Check buffer and disk space constraints
            can_download, reason = self.check_storage_limits(expected_size)
            if not can_download:
                break

            success = self.download_file(recording_dict)
            if not success:
                logger.warning(f"Stopping current sync cycle due to download error on '{recording_dict['filename']}'.")
                break

            current_buf_gb = self.get_current_local_buffer_size() / (1024 ** 3)
            max_buf_gb = self.config.MAX_BUFFER_BYTES / (1024 ** 3)
            storage_logger.info(f"Local buffer: {current_buf_gb:.2f} GB / {max_buf_gb:.2f} GB")

            # Invoke callback immediately after each file download completes
            if on_file_downloaded:
                try:
                    on_file_downloaded()
                except Exception as cb_exc:
                    logger.error(f"Callback error after downloading '{recording_dict['filename']}': {cb_exc}")

