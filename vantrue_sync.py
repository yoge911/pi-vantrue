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
    """HTML parser to extract file hyperlinks and subfolder directory links from HTTP listings."""

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.file_links: List[str] = []
        self.dir_links: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]):
        if tag.lower() == "a":
            for name, val in attrs:
                if name.lower() == "href" and val:
                    if val in ("../", "..", "./", "."):
                        continue
                    full_url = urllib.parse.urljoin(self.base_url, val)
                    unquoted_path = urllib.parse.unquote(full_url)
                    path_lower = unquoted_path.lower()

                    if any(path_lower.endswith(ext) for ext in Config.SUPPORTED_EXTENSIONS):
                        if full_url not in self.file_links:
                            self.file_links.append(full_url)
                    elif val.endswith("/") or (not Path(unquoted_path).suffix and full_url.startswith(self.base_url)):
                        if full_url not in self.dir_links and full_url != self.base_url:
                            self.dir_links.append(full_url)


def classify_file_priority(remote_url: str, filename: str) -> int:
    """
    Classify recording priority (0=highest) based primarily on directory path,
    falling back to filename patterns and extension matching.

    Primary directory classification (authoritative):
      - /Event/  -> Priority 0 (EVENT)
      - /Normal/ -> Priority 1 (NORMAL)
      - /GPS/    -> Priority 2 (GPS)
      - /Photo/  -> Priority 3 (PHOTO)

    Fallback classification:
      - _E_, _EV_, _EMG_, event -> Priority 0
      - _N_, _NOR_, normal      -> Priority 1
      - .gps, .dat, .log, gps   -> Priority 2
      - .jpg, .jpeg, .png, photo -> Priority 3
      - Other                    -> Priority 4
    """
    parsed_path = urllib.parse.urlparse(remote_url).path
    unquoted_path = urllib.parse.unquote(parsed_path)
    path_parts = [p.lower() for p in unquoted_path.split("/") if p]
    fn_lower = filename.lower()

    # Primary: Authoritative Directory Path check
    if "event" in path_parts:
        return Config.PRIORITY_EVENT
    if "normal" in path_parts:
        return Config.PRIORITY_NORMAL
    if "gps" in path_parts:
        return Config.PRIORITY_GPS
    if any(p in path_parts for p in ["photo", "photos", "picture", "pictures"]):
        return Config.PRIORITY_PHOTO

    # Secondary: Fallback Filename / Extension check
    fn_stem = Path(fn_lower).stem
    stem_tokens = [t for t in re.split(r"[_-]", fn_stem) if t]

    if any(t in stem_tokens for t in ["e", "ev", "emg", "event"]) or "event" in fn_lower:
        return Config.PRIORITY_EVENT
    if any(t in stem_tokens for t in ["n", "nor", "normal"]) or "normal" in fn_lower:
        return Config.PRIORITY_NORMAL
    if fn_lower.endswith((".gps", ".dat", ".log")) or "gps" in fn_lower:
        return Config.PRIORITY_GPS
    if fn_lower.endswith((".jpg", ".jpeg", ".png")) or "photo" in fn_lower:
        return Config.PRIORITY_PHOTO

    return Config.PRIORITY_OTHER


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
        self.last_discovery_time: float = 0.0
        self.is_discovery_running: bool = False

    def check_camera_endpoint_quick(self) -> bool:
        """Quick HTTP GET probe to verify camera endpoint is reachable before querying SQLite work."""
        try:
            req = urllib.request.Request(self.config.VANTRUE_BASE_URL, headers={"User-Agent": "VantruePiAutomation/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def get_remote_file_size(self, remote_url: str) -> int:
        """Perform a HEAD request to observe remote file size in bytes."""
        try:
            req = urllib.request.Request(remote_url, method="HEAD", headers={"User-Agent": "VantruePiAutomation/1.0"})
            with urllib.request.urlopen(req, timeout=self.config.HTTP_TIMEOUT) as head_resp:
                content_length = head_resp.headers.get("Content-Length")
                if content_length and content_length.isdigit():
                    return int(content_length)
        except Exception:
            pass
        return 0

    def is_remote_file_stable(self, recording: Dict) -> bool:
        """
        Verify file completeness/stability via 2 matching size observations separated by STABILITY_CHECK_DELAY.
        Returns False if size changes during observation window (indicating active writing).
        """
        remote_url = recording["remote_url"]
        filename = recording["filename"]
        delay = getattr(self.config, "STABILITY_CHECK_DELAY", 2.0)

        # Observation 1
        size_a = self.get_remote_file_size(remote_url)
        if size_a <= 0:
            size_a = recording.get("file_size", 0)

        if size_a <= 0:
            logger.debug(f"Unable to observe size for '{filename}'; treating as candidate stable.")
            return True

        if delay <= 0:
            return True

        time.sleep(delay)

        # Observation 2
        size_b = self.get_remote_file_size(remote_url)
        if size_b <= 0:
            size_b = size_a  # Fallback if second HEAD request transiently failed

        if size_a != size_b:
            logger.info(
                f"File '{filename}' size changed ({size_a} B -> {size_b} B) over {delay}s delay. "
                f"File is actively being written by dashcam. Skipping for now."
            )
            return False

        return True

    def scan_remote_recordings(self) -> List[Dict]:
        """Query HTTP directory listing (including subfolders) and return discovered file metadata with priority."""
        url = self.config.VANTRUE_BASE_URL
        logger.info(f"Querying Vantrue recordings directory from endpoint {url}...")

        urls_to_visit = [url]
        visited_urls = set()
        discovered = []
        discovered_urls = set()

        while urls_to_visit and len(visited_urls) < 10:
            current_url = urls_to_visit.pop(0)
            if current_url in visited_urls:
                continue
            visited_urls.add(current_url)

            try:
                req = urllib.request.Request(current_url, headers={"User-Agent": "VantruePiAutomation/1.0"})
                with urllib.request.urlopen(req, timeout=self.config.HTTP_TIMEOUT) as response:
                    html_content = response.read().decode("utf-8", errors="ignore")
            except Exception as exc:
                logger.debug(f"Failed to fetch directory listing at {current_url}: {exc}")
                continue

            parser = VantrueHTMLParser(current_url)
            parser.feed(html_content)

            for file_url in parser.file_links:
                if file_url in discovered_urls:
                    continue
                discovered_urls.add(file_url)

                filename = urllib.parse.unquote(Path(urllib.parse.urlparse(file_url).path).name)
                timestamp = extract_timestamp_from_filename(filename)
                priority = classify_file_priority(file_url, filename)

                file_size = self.get_remote_file_size(file_url)

                discovered.append({
                    "remote_url": file_url,
                    "filename": filename,
                    "file_size": file_size,
                    "recording_timestamp": timestamp,
                    "priority": priority,
                })

            for dir_url in parser.dir_links:
                if dir_url not in visited_urls and dir_url not in urls_to_visit:
                    urls_to_visit.append(dir_url)

        logger.info(f"Discovered {len(discovered)} supported files on dashcam HTTP server.")
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

    def run_sync(self, on_file_downloaded: Optional[Callable[[], None]] = None, force_rescan: bool = False):
        """
        Execute decoupled sync workflow:
          1. Check SQLite for pending downloads (ordered Newest-First).
          2. If pending items exist and discovery timer is active (and not force_rescan):
             Verify camera endpoint reachability -> Process SQLite pending items directly.
          3. If pending queue is empty or discovery timer elapsed (or force_rescan):
             Execute scan_remote_recordings() to discover newly generated recordings.
        """
        consecutive_404_count = 0

        while True:
            pending = self.db.get_pending_downloads()

            now = time.time()
            interval = getattr(self.config, "DASHCAM_DISCOVERY_INTERVAL", 180.0)
            time_since_discovery = now - self.last_discovery_time

            need_discovery = (
                force_rescan
                or self.last_discovery_time == 0.0
                or time_since_discovery >= interval
                or not pending
                or consecutive_404_count >= 2
            )

            if need_discovery:
                logger.info("Executing remote file discovery scan...")
                self.is_discovery_running = True
                try:
                    discovered = self.scan_remote_recordings()
                    self.last_discovery_time = time.time()
                    force_rescan = False
                    consecutive_404_count = 0
                except Exception as exc:
                    self.is_discovery_running = False
                    logger.info(f"Vantrue HTTP endpoint unreachable: {exc}. Will retry in next cycle.")
                    return
                finally:
                    self.is_discovery_running = False

                if discovered:
                    self.db.register_recordings(discovered)

                pending = self.db.get_pending_downloads()

            if not pending:
                logger.info("All discovered videos have been downloaded and queue is empty.")
                return

            if not self.check_camera_endpoint_quick():
                logger.info("Vantrue HTTP endpoint unreachable. Will retry in next cycle.")
                return

            target_rec = None
            for rec in pending:
                recording_dict = dict(rec)
                if self.is_remote_file_stable(recording_dict):
                    target_rec = recording_dict
                    break
                else:
                    logger.info(
                        f"File '{recording_dict['filename']}' (Priority {recording_dict.get('priority')}) "
                        f"is currently being written/unstable. Skipping for next eligible file."
                    )

            if not target_rec:
                logger.info("No eligible stable files ready for download in this cycle.")
                return

            expected_size = target_rec.get("file_size", 0)

            can_download, reason = self.check_storage_limits(expected_size)
            if not can_download:
                logger.warning(f"Storage limit reached ({reason}). Pausing download cycle.")
                return

            success = self.download_file(target_rec)
            if not success:
                logger.warning(f"Stopping current sync cycle due to download error on '{target_rec['filename']}'.")
                return

            current_buf_gb = self.get_current_local_buffer_size() / (1024 ** 3)
            max_buf_gb = self.config.MAX_BUFFER_BYTES / (1024 ** 3)
            storage_logger.info(f"Local buffer: {current_buf_gb:.2f} GB / {max_buf_gb:.2f} GB")

            if on_file_downloaded:
                try:
                    on_file_downloaded()
                except Exception as cb_exc:
                    logger.error(f"Callback error after downloading '{target_rec['filename']}': {cb_exc}")

