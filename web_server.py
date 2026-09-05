import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse

import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Dict, List, Optional, Tuple

from config import Config
from db import SyncDB
from logger import setup_logging
from retention import RetentionManager

logger = logging.getLogger("web")


class CloudStreamCache:
    """Thread-safe local disk cache for initial byte ranges of cloud-synced videos.

    Eliminates high-latency subprocess overhead during iOS AVPlayer initial metadata range probes.
    """

    def __init__(
        self,
        cache_dir: Path = Path("/tmp/cloud_stream_cache"),
        prefetch_bytes: int = 4 * 1024 * 1024,
        max_files: int = 20,
    ):
        self.cache_dir = cache_dir
        self.prefetch_bytes = prefetch_bytes
        self.max_files = max_files
        self.lock = threading.Lock()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _clean_old_entries(self):
        """Clean up oldest cached chunk files if max file limit is exceeded."""
        try:
            files = sorted(
                self.cache_dir.glob("*.part0"), key=lambda p: p.stat().st_mtime
            )
            while len(files) >= self.max_files:
                oldest = files.pop(0)
                try:
                    oldest.unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception as exc:
            logger.debug(f"Cloud stream cache cleanup error: {exc}")

    def get_cached_chunk(
        self,
        filename: str,
        remote_target: str,
        start_byte: int,
        end_byte: int,
        total_file_size: int,
    ) -> Optional[bytes]:
        """Return cached bytes if request falls within cached initial range [start_byte, end_byte], else None."""
        if total_file_size <= 0:
            return None

        safe_filename = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", filename)
        cache_file = self.cache_dir / f"{safe_filename}.part0"
        needed_prefetch = min(self.prefetch_bytes, total_file_size)

        with self.lock:
            if not cache_file.exists() or cache_file.stat().st_size < needed_prefetch:
                if start_byte < self.prefetch_bytes:
                    self._clean_old_entries()
                    temp_file = (
                        self.cache_dir
                        / f"{safe_filename}.part0.tmp.{os.getpid()}_{threading.get_ident()}"
                    )
                    cmd = [
                        "rclone",
                        "cat",
                        "--offset",
                        "0",
                        "--count",
                        str(needed_prefetch),
                        remote_target,
                    ]
                    try:
                        logger.info(
                            f"Prefetching initial {needed_prefetch} bytes for cloud video '{filename}' into cache..."
                        )
                        res = subprocess.run(cmd, capture_output=True, timeout=15)
                        if res.returncode == 0 and len(res.stdout) > 0:
                            with open(temp_file, "wb") as f:
                                f.write(res.stdout)
                            temp_file.replace(cache_file)
                        else:
                            if temp_file.exists():
                                temp_file.unlink(missing_ok=True)
                    except Exception as exc:
                        logger.warning(
                            f"Failed to prefetch cloud stream chunk for '{filename}': {exc}"
                        )
                        if temp_file.exists():
                            temp_file.unlink(missing_ok=True)

        if cache_file.exists():
            try:
                cached_size = cache_file.stat().st_size
                if start_byte < cached_size and end_byte < cached_size:
                    with open(cache_file, "rb") as f:
                        f.seek(start_byte)
                        read_len = end_byte - start_byte + 1
                        return f.read(read_len)
            except OSError as exc:
                logger.warning(
                    f"Error reading cloud stream cache file '{cache_file}': {exc}"
                )

        return None


cloud_stream_cache = CloudStreamCache()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Multithreaded HTTP Server handling non-blocking streaming and API calls."""

    daemon_threads = True


def get_network_interface_info(interface: str) -> Dict[str, str]:
    """Get active status and IPv4 address for a network interface."""
    info = {"status": "disconnected", "ip": "N/A"}
    try:
        res = subprocess.run(
            ["ip", "-4", "addr", "show", interface],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if res.returncode == 0 and res.stdout:
            for line in res.stdout.splitlines():
                line = line.strip()
                if line.startswith("inet "):
                    ip_cidr = line.split()[1]
                    info["ip"] = ip_cidr.split("/")[0]
                    info["status"] = "connected"
                    break
    except Exception:
        pass
    return info


def check_internet_quick() -> bool:
    """Quick non-blocking internet connectivity probe."""
    try:
        req = urllib.request.Request(
            Config.INTERNET_CHECK_URL,
            headers={"User-Agent": "VantruePiAutomation/1.0"},
        )
        with urllib.request.urlopen(req, timeout=1) as resp:
            return resp.status in (200, 204)
    except Exception:
        return False


def check_dashcam_quick() -> bool:
    """Fast non-blocking socket check for dashcam reachability."""
    try:
        url = urllib.parse.urlparse(Config.VANTRUE_BASE_URL)
        host = url.hostname or "192.168.1.254"
        port = url.port or 80
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except Exception:
        return False


def categorize_filename(filename: str) -> Dict[str, str]:
    """Categorize Vantrue dashcam filename into type (Normal/Event/Photo) and camera position (Front/Rear/Interior)."""
    name_upper = filename.upper()
    cat_type = "Normal"
    if "_E_" in name_upper or "EVENT" in name_upper:
        cat_type = "Event"
    elif name_upper.endswith(".JPG") or name_upper.endswith(".JPEG") or "_P_" in name_upper:
        cat_type = "Photo"

    position = "Front"
    if "_B." in name_upper or "_B_" in name_upper or "REAR" in name_upper:
        position = "Rear"
    elif "_C." in name_upper or "_C_" in name_upper or "INT" in name_upper:
        position = "Interior"
    elif "_A." in name_upper or "_A_" in name_upper or "FRONT" in name_upper:
        position = "Front"

    return {"type": cat_type, "position": position, "label": f"{cat_type} / {position}"}


class VantrueWebHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler for Vantrue Pi Web Dashboard and Streaming Proxy."""

    def log_message(self, format_str: str, *args: float):
        """Override log_message for unified python logging."""
        logger.debug(f"{self.address_string()} - {format_str % args}")

    def _send_json(self, status_code: int, data: Dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        if path in ("/", "/index.html"):
            self._handle_serve_dashboard()
        elif path == "/api/status":
            self._handle_api_status()
        elif path == "/api/videos":
            self._handle_api_videos(parsed_url.query)
        elif path.startswith("/stream/"):
            filename = urllib.parse.unquote(path[len("/stream/") :])
            self._handle_stream_video(filename, parsed_url.query)
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Resource not found")

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        if path == "/api/cleanup":
            self._handle_api_cleanup()
        elif path == "/api/rescan":
            self._handle_api_rescan()
        elif path == "/api/backfill":
            self._handle_api_backfill()
        elif path in ("/api/videos/delete", "/api/delete"):
            self._handle_api_delete_videos()
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Endpoint not found")

    def _handle_serve_dashboard(self):
        html_content = HTML_TEMPLATE.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html_content)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(html_content)

    def _handle_api_status(self):
        try:
            db = SyncDB(Config.DB_PATH)
            stats = db.get_stats()

            # Storage calculations
            total_b, used_b, free_b = 0, 0, 0
            if Config.LOCAL_DOWNLOAD_DIR.exists():
                usage = shutil.disk_usage(Config.LOCAL_DOWNLOAD_DIR)
                total_b, used_b, free_b = usage.total, usage.used, usage.free

            wlan0_info = get_network_interface_info(Config.VANTRUE_INTERFACE)
            wlan1_info = get_network_interface_info(Config.INTERNET_INTERFACE)

            status_data = {
                "hostname": socket.gethostname(),
                "storage": {
                    "total_gb": round(total_b / (1024 ** 3), 2),
                    "used_gb": round(used_b / (1024 ** 3), 2),
                    "free_gb": round(free_b / (1024 ** 3), 2),
                    "min_free_space_gb": Config.MIN_FREE_SPACE_GB,
                    "percent_used": round((used_b / total_b * 100), 1) if total_b else 0,
                },
                "queue": {
                    "pending_download_count": stats.get("pending_download_count", 0),
                    "pending_event_count": stats.get("pending_event_count", 0),
                    "pending_normal_count": stats.get("pending_normal_count", 0),
                    "pending_upload_count": stats.get("pending_upload_count", 0),
                },
                "videos": {
                    "local_count": stats.get("local_count", 0),
                    "local_size_mb": round((stats.get("local_size") or 0) / (1024 * 1024), 1),
                    "uploaded_count": stats.get("uploaded_count", 0),
                    "pending_upload_count": stats.get("pending_upload_count", 0),
                    "total_discovered": stats.get("total_discovered", 0),
                },
                "network": {
                    "wlan0": {
                        "interface": Config.VANTRUE_INTERFACE,
                        "status": wlan0_info.get("status", "disconnected"),
                        "ip": wlan0_info.get("ip", "N/A"),
                        "dashcam_reachable": check_dashcam_quick(),
                    },
                    "wlan1": {
                        "interface": Config.INTERNET_INTERFACE,
                        "status": wlan1_info.get("status", "disconnected"),
                        "ip": wlan1_info.get("ip", "N/A"),
                        "internet_reachable": check_internet_quick(),
                    },
                },
                "last_uploaded_at": stats.get("last_uploaded_at"),
            }
            self._send_json(HTTPStatus.OK, status_data)
        except Exception as exc:
            logger.error(f"Error in _handle_api_status: {exc}", exc_info=True)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _handle_api_videos(self, query_str: str):
        try:
            params = urllib.parse.parse_qs(query_str)
            filter_status = params.get("status", ["all"])[0]
            sort_order = params.get("sort", ["desc"])[0]

            db = SyncDB(Config.DB_PATH)
            records = db.get_all_recordings(
                filter_status=filter_status, sort_desc=(sort_order == "desc")
            )

            base_dir = Config.LOCAL_DOWNLOAD_DIR.resolve()
            video_list = []
            for r in records:
                fn = r["filename"]
                local_path = base_dir / fn
                is_present = local_path.exists() and local_path.is_file()

                keys = r.keys() if hasattr(r, "keys") else []
                drive_id = r["drive_file_id"] if ("drive_file_id" in keys and r["drive_file_id"]) else None
                cloud_play_url = f"https://drive.google.com/file/d/{drive_id}/view" if drive_id else None
                cloud_embed_url = f"https://drive.google.com/file/d/{drive_id}/preview" if drive_id else None
                cloud_direct_url = f"https://drive.google.com/uc?export=download&id={drive_id}" if drive_id else None

                db_status = r["status"]
                is_synced = db_status in ("uploaded", "deleted") or bool(drive_id)

                # Explicit file state classification (dashcam, local, local+cloud, cloud, missing, purged)
                if db_status == "discovered" and not is_present:
                    file_state = "dashcam"
                elif is_present and is_synced:
                    file_state = "local+cloud"
                elif is_present and not is_synced:
                    file_state = "local"
                elif not is_present and is_synced:
                    file_state = "cloud"
                elif not is_present and db_status == "downloaded":
                    file_state = "missing"
                elif not is_present and db_status == "deleted" and not is_synced:
                    file_state = "purged"
                else:
                    file_state = db_status

                cat_info = categorize_filename(fn)
                raw_size = r["file_size"] or 0
                video_list.append(
                    {
                        "filename": fn,
                        "recording_timestamp": r["recording_timestamp"] if "recording_timestamp" in keys else fn,
                        "file_size": raw_size,
                        "file_size_mb": round(raw_size / (1024 * 1024), 1),
                        "status": db_status,
                        "file_state": file_state,
                        "uploaded_at": r["uploaded_at"] if ("uploaded_at" in keys and r["uploaded_at"]) else None,
                        "drive_file_id": drive_id,
                        "cloud_play_url": cloud_play_url,
                        "cloud_embed_url": cloud_embed_url,
                        "cloud_direct_url": cloud_direct_url,
                        "local_present": is_present,
                        "category": cat_info.get("label", "Normal / Front"),
                        "category_type": cat_info.get("type", "Normal"),
                        "category_position": cat_info.get("position", "Front"),
                        "stream_url": f"/stream/{urllib.parse.quote(fn)}" if is_present else None,
                    }
                )

            self._send_json(HTTPStatus.OK, {"videos": video_list, "count": len(video_list)})
        except Exception as exc:
            logger.error(f"Error in _handle_api_videos: {exc}", exc_info=True)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"videos": [], "count": 0, "error": str(exc)})

    def _handle_api_cleanup(self):
        logger.info("Manual cleanup requested via Web API.")
        retention_mgr = RetentionManager(Config)
        success, status_code = retention_mgr.cleanup_uploaded_files_if_needed()

        usage = shutil.disk_usage(Config.LOCAL_DOWNLOAD_DIR)
        self._send_json(
            HTTPStatus.OK,
            {
                "status": "success" if success else "warning",
                "code": status_code,
                "free_gb": round(usage.free / (1024 ** 3), 2),
                "min_free_space_gb": Config.MIN_FREE_SPACE_GB,
            },
        )

    def _handle_api_rescan(self):
        logger.info("Manual remote rescan requested via Web API.")
        try:
            from vantrue_sync import VantrueSyncEngine
            engine = VantrueSyncEngine(Config)
            engine.run_sync(force_rescan=True)
            self._send_json(HTTPStatus.OK, {"status": "success", "message": "Discovery rescan completed."})
        except Exception as exc:
            logger.error(f"Manual rescan error: {exc}")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"status": "error", "message": str(exc)})

    def _handle_api_backfill(self):
        logger.info("Manual Drive ID backfill requested via Web API.")
        try:
            from uploader import VantrueUploader
            uploader = VantrueUploader(Config)
            count_before = len(uploader.db.get_recordings_missing_drive_id(limit=5000))
            uploader.backfill_missing_drive_ids(max_batch=50)
            count_after = len(uploader.db.get_recordings_missing_drive_id(limit=5000))
            processed = count_before - count_after
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "success",
                    "processed": processed,
                    "remaining": count_after,
                    "message": f"Backfilled {processed} Google Drive file IDs ({count_after} remaining).",
                },
            )
        except Exception as exc:
            logger.error(f"Manual backfill error: {exc}")
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"status": "error", "message": str(exc)},
            )

    def _handle_api_delete_videos(self):
        logger.info("Bulk video deletion requested via Web API.")
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length <= 0:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"status": "error", "message": "Missing request payload."},
                )
                return

            body = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(body)
            filenames = data.get("filenames", [])
            scope = data.get("scope", "local")  # "local", "cloud", "both"

            if not filenames:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"status": "error", "message": "No filenames specified for deletion."},
                )
                return

            from uploader import VantrueUploader

            uploader = VantrueUploader(Config)
            db = SyncDB(Config.DB_PATH)
            base_dir = Config.LOCAL_DOWNLOAD_DIR.resolve()

            results = []
            successful_count = 0
            failed_count = 0

            for fn in filenames:
                record = db.get_recording_by_filename(fn)
                if not record:
                    results.append(
                        {
                            "filename": fn,
                            "status": "error",
                            "message": "Record not found in database.",
                        }
                    )
                    failed_count += 1
                    continue

                drive_id = (
                    record["drive_file_id"]
                    if ("drive_file_id" in record.keys() and record["drive_file_id"])
                    else None
                )
                target_path = (base_dir / fn).resolve()

                local_success = True
                cloud_success = True
                msg_parts = []

                # A. Delete Local Pi Copy
                if scope in ("local", "both"):
                    if (
                        target_path.exists()
                        and target_path.is_file()
                        and base_dir in target_path.parents
                    ):
                        try:
                            target_path.unlink()
                            msg_parts.append("Local Pi copy deleted")
                        except Exception as exc:
                            local_success = False
                            msg_parts.append(f"Local deletion failed ({exc})")
                    else:
                        msg_parts.append("Local Pi copy already absent")

                # B. Delete Cloud / Google Drive Copy
                if scope in ("cloud", "both"):
                    if drive_id or record["status"] in ("uploaded", "deleted"):
                        ok, cloud_msg = uploader.delete_cloud_file(
                            fn, drive_file_id=drive_id
                        )
                        if ok:
                            cloud_success = True
                            msg_parts.append("Cloud copy deleted")
                        else:
                            cloud_success = False
                            msg_parts.append(cloud_msg)
                    else:
                        msg_parts.append("Cloud copy not present")

                # C. Reconcile DB status based on results
                is_overall_success = local_success and cloud_success
                if is_overall_success:
                    successful_count += 1
                    new_is_local = target_path.exists() and target_path.is_file()

                    if scope == "both":
                        db.update_recording_status(
                            fn, status="deleted", clear_drive_id=True
                        )
                    elif scope == "local":
                        if record["status"] == "uploaded":
                            db.update_recording_status(
                                fn, status="deleted", clear_drive_id=False
                            )
                        elif record["status"] == "downloaded":
                            db.update_recording_status(
                                fn, status="discovered", clear_drive_id=False
                            )
                    elif scope == "cloud":
                        if new_is_local:
                            db.update_recording_status(
                                fn, status="downloaded", clear_drive_id=True
                            )
                        else:
                            db.update_recording_status(
                                fn, status="deleted", clear_drive_id=True
                            )

                    results.append(
                        {
                            "filename": fn,
                            "status": "success",
                            "message": " / ".join(msg_parts),
                        }
                    )
                else:
                    failed_count += 1
                    # Partial result reconciliation
                    if local_success and scope in ("local", "both"):
                        if record["status"] == "uploaded":
                            db.update_recording_status(
                                fn, status="deleted", clear_drive_id=False
                            )
                    if cloud_success and scope in ("cloud", "both"):
                        db.update_recording_status(
                            fn, status=record["status"], clear_drive_id=True
                        )

                    results.append(
                        {
                            "filename": fn,
                            "status": "warning"
                            if (local_success or cloud_success)
                            else "error",
                            "message": " / ".join(msg_parts),
                        }
                    )

            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "success" if failed_count == 0 else "partial",
                    "total": len(filenames),
                    "successful": successful_count,
                    "failed": failed_count,
                    "results": results,
                },
            )
        except Exception as exc:
            logger.error(f"Bulk delete handler error: {exc}", exc_info=True)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"status": "error", "message": str(exc)},
            )

    def _handle_stream_video(self, filename: str, query_str: str = ""):
        """
        Stream MP4 video file supporting HTTP Byte-Range requests for iOS Safari / Chrome seeking.
        If file exists locally and source!=cloud, stream directly from disk.
        If file is cloud-synced (uploaded/deleted) or source=cloud, proxy stream from Google Drive via rclone cat with Byte-Range support.
        Includes strict path traversal security checks.
        """
        params = urllib.parse.parse_qs(query_str)
        force_cloud = params.get("source", ["auto"])[0] == "cloud"

        base_dir = Config.LOCAL_DOWNLOAD_DIR.resolve()
        target_path = (base_dir / filename).resolve()

        content_type = "video/mp4"
        fn_lower = filename.lower()
        if fn_lower.endswith((".jpg", ".jpeg")):
            content_type = "image/jpeg"
        elif fn_lower.endswith(".png"):
            content_type = "image/png"
        elif fn_lower.endswith((".gps", ".dat", ".log", ".txt")):
            content_type = "text/plain"

        # --- PATH A: FILE IS PRESENT ON LOCAL DISK ---
        if not force_cloud and target_path.exists() and target_path.is_file() and base_dir in target_path.parents:
            file_size = target_path.stat().st_size
            range_header = self.headers.get("Range")

            if range_header:
                try:
                    byte_range = range_header.replace("bytes=", "").split("-")
                    start_byte = int(byte_range[0])
                    end_byte = int(byte_range[1]) if byte_range[1] else file_size - 1

                    if start_byte >= file_size or end_byte >= file_size or start_byte > end_byte:
                        self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                        self.send_header("Content-Range", f"bytes */{file_size}")
                        self.end_headers()
                        return

                    chunk_len = end_byte - start_byte + 1
                    self.send_response(HTTPStatus.PARTIAL_CONTENT)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Range", f"bytes {start_byte}-{end_byte}/{file_size}")
                    self.send_header("Content-Length", str(chunk_len))
                    self.send_header("Cache-Control", "public, max-age=3600")
                    self.end_headers()

                    with open(target_path, "rb") as f:
                        f.seek(start_byte)
                        bytes_remaining = chunk_len
                        buffer_size = 64 * 1024
                        while bytes_remaining > 0:
                            read_size = min(buffer_size, bytes_remaining)
                            data = f.read(read_size)
                            if not data:
                                break
                            self.wfile.write(data)
                            bytes_remaining -= len(data)
                    return

                except (ValueError, IndexError, OSError) as exc:
                    logger.debug(f"Range parsing or socket error for '{filename}': {exc}")
                    return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(file_size))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()

            try:
                with open(target_path, "rb") as f:
                    buffer_size = 64 * 1024
                    while True:
                        data = f.read(buffer_size)
                        if not data:
                            break
                        self.wfile.write(data)
            except OSError:
                pass
            return

        # --- PATH B: FILE IS CLOUD-SYNCED (PI PROXIED STREAM FROM GOOGLE DRIVE VIA RCLONE CAT) ---
        db = SyncDB(Config.DB_PATH)
        record = db.get_recording_by_filename(filename)
        if not record or record["status"] not in ("uploaded", "deleted"):
            self.send_error(HTTPStatus.NOT_FOUND, "Video file not found")
            return

        file_size = record["file_size"] if record["file_size"] else 0
        remote_target = f"{Config.RCLONE_REMOTE}{Config.RCLONE_DESTINATION}/{filename}"
        range_header = self.headers.get("Range")

        if range_header and file_size > 0:
            try:
                byte_range = range_header.replace("bytes=", "").split("-")
                start_byte = int(byte_range[0])
                end_byte = int(byte_range[1]) if byte_range[1] else file_size - 1

                if start_byte >= file_size or end_byte >= file_size or start_byte > end_byte:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.end_headers()
                    return

                chunk_len = end_byte - start_byte + 1

                # Check initial range disk cache first for fast metadata access (iOS AVPlayer optimization)
                cached_bytes = cloud_stream_cache.get_cached_chunk(
                    filename, remote_target, start_byte, end_byte, file_size
                )
                if cached_bytes is not None:
                    self.send_response(HTTPStatus.PARTIAL_CONTENT)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Range", f"bytes {start_byte}-{end_byte}/{file_size}")
                    self.send_header("Content-Length", str(len(cached_bytes)))
                    self.send_header("Cache-Control", "public, max-age=3600")
                    self.end_headers()
                    self.wfile.write(cached_bytes)
                    return

                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Type", content_type)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start_byte}-{end_byte}/{file_size}")
                self.send_header("Content-Length", str(chunk_len))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()

                cmd = [
                    "rclone",
                    "cat",
                    "--offset",
                    str(start_byte),
                    "--count",
                    str(chunk_len),
                    remote_target,
                ]

                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                try:
                    while True:
                        chunk = proc.stdout.read(64 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                except (OSError, BrokenPipeError):
                    pass
                finally:
                    proc.kill()
                    proc.wait()
                return

            except Exception as exc:
                logger.warning(f"Cloud stream range error for '{filename}': {exc}")
                return

        # Full stream request
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "public, max-age=3600")
        if file_size > 0:
            self.send_header("Content-Length", str(file_size))
        self.end_headers()

        cmd = ["rclone", "cat", remote_target]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (OSError, BrokenPipeError):
            pass
        finally:
            proc.kill()
            proc.wait()


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Vantrue Pi Dashboard</title>
    <style>
        :root {
            --bg-color: #0f172a;
            --card-bg: #1e293b;
            --text-color: #f8fafc;
            --text-muted: #94a3b8;
            --accent-color: #38bdf8;
            --accent-green: #22c55e;
            --accent-warn: #eab308;
            --accent-alert: #ef4444;
            --border-color: #334155;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            padding: 16px;
            max-width: 800px;
            margin: 0 auto;
        }
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-bottom: 16px;
            border-bottom: 1px solid var(--border-color);
            margin-bottom: 16px;
        }
        h1 { font-size: 1.4rem; color: var(--accent-color); }
        .refresh-btn {
            background: var(--card-bg);
            color: var(--accent-color);
            border: 1px solid var(--border-color);
            padding: 8px 14px;
            border-radius: 8px;
            font-weight: 600;
            cursor: pointer;
        }
        .card {
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 16px;
        }
        .card-title {
            font-size: 0.9rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-muted);
            margin-bottom: 12px;
        }
        .progress-bar-bg {
            background: #0f172a;
            border-radius: 8px;
            height: 12px;
            width: 100%;
            overflow: hidden;
            margin-top: 8px;
        }
        .progress-bar-fill {
            background: var(--accent-color);
            height: 100%;
            width: 0%;
            transition: width 0.3s ease;
        }
        .status-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
        }
        .stat-box {
            background: #0f172a;
            padding: 12px;
            border-radius: 8px;
            border: 1px solid var(--border-color);
        }
        .stat-label { font-size: 0.8rem; color: var(--text-muted); }
        .stat-value { font-size: 1.1rem; font-weight: bold; margin-top: 4px; }
        .pill {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 0.75rem;
            font-weight: 600;
        }
        .pill-info { background: rgba(56, 189, 248, 0.2); color: var(--accent-color); }
        .pill-green { background: rgba(34, 197, 94, 0.2); color: var(--accent-green); }
        .pill-warn { background: rgba(234, 179, 8, 0.2); color: var(--accent-warn); }
        .pill-alert { background: rgba(239, 68, 68, 0.2); color: var(--accent-alert); }
        
        .filter-controls {
            display: flex;
            gap: 8px;
            margin-bottom: 12px;
        }
        .filter-btn {
            background: #0f172a;
            color: var(--text-muted);
            border: 1px solid var(--border-color);
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 0.85rem;
            cursor: pointer;
        }
        .filter-btn.active {
            background: var(--accent-color);
            color: #0f172a;
            border-color: var(--accent-color);
            font-weight: bold;
        }
        .video-item {
            border-bottom: 1px solid var(--border-color);
            padding: 12px 0;
        }
        .video-item:last-child { border-bottom: none; }
        .video-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 4px;
        }
        .video-title { font-weight: 600; word-break: break-all; }
        .video-meta {
            font-size: 0.8rem;
            color: var(--text-muted);
            display: flex;
            gap: 12px;
            margin-top: 4px;
        }
        .action-btn {
            background: var(--accent-color);
            color: #0f172a;
            border: none;
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 0.85rem;
            font-weight: bold;
            cursor: pointer;
            text-decoration: none;
            display: inline-block;
        }
        .local-btn {
            background: var(--accent-green);
            color: #0f172a;
        }
        .cloud-btn {
            background: var(--accent-color);
            color: #0f172a;
        }
        .link-btn {
            background: #334155;
            color: var(--text-color);
        }
        video {
            width: 100%;
            max-height: 360px;
            border-radius: 8px;
            margin-top: 8px;
            background: #000;
        }
    </style>
</head>
<body>
    <header>
        <h1>Vantrue Pi Cache</h1>
        <button class="refresh-btn" onclick="loadAll()">Refresh</button>
    </header>

    <div class="card">
        <div class="card-title">Storage Rolling Cache</div>
        <div id="storage-summary">Loading storage...</div>
        <div class="progress-bar-bg">
            <div id="storage-bar" class="progress-bar-fill"></div>
        </div>
        <div style="display:flex; justify-content:space-between; margin-top:8px;">
            <button class="filter-btn" onclick="triggerCleanup()">Cleanup Now</button>
        </div>
    </div>

    <div class="card">
        <div class="card-title">Download Queue & Discovery</div>
        <div class="status-grid">
            <div class="stat-box">
                <div class="stat-label">Pending Downloads</div>
                <div id="queue-pending" class="stat-value">...</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">Pending Breakdown</div>
                <div id="queue-breakdown" class="stat-value">...</div>
            </div>
        </div>
        <div style="display:flex; justify-content:flex-end; margin-top:12px;">
            <button class="filter-btn" onclick="triggerRescan()">Force Rescan</button>
        </div>
    </div>

    <div class="card">
        <div class="card-title">Network & System Status</div>
        <div class="status-grid">
            <div class="stat-box">
                <div class="stat-label">wlan0 (Dashcam)</div>
                <div id="net-wlan0" class="stat-value">...</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">wlan1 (Hotspot / SSH)</div>
                <div id="net-wlan1" class="stat-value">...</div>
            </div>
        </div>
    </div>

    <div class="card">
        <div class="card-title">Video Library</div>
        
        <!-- Filter Controls: Camera & Location Status -->
        <div style="display:flex; flex-direction:column; gap:8px; margin-bottom:12px;">
            <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                <span style="font-size:0.8rem; color:var(--text-muted); min-width:65px;">Camera:</span>
                <div class="filter-controls-camera" style="display:flex; gap:6px; flex-wrap:wrap;">
                    <button class="filter-btn active" onclick="setCameraFilter('all', this)">All</button>
                    <button class="filter-btn" onclick="setCameraFilter('Front', this)">Front</button>
                    <button class="filter-btn" onclick="setCameraFilter('Rear', this)">Rear</button>
                    <button class="filter-btn" onclick="setCameraFilter('Interior', this)">Interior</button>
                </div>
            </div>
            
            <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                <span style="font-size:0.8rem; color:var(--text-muted); min-width:65px;">Status:</span>
                <div class="filter-controls-status" style="display:flex; gap:6px; flex-wrap:wrap;">
                    <button class="filter-btn active" onclick="setStatusFilter('all', this)">All</button>
                    <button class="filter-btn" onclick="setStatusFilter('uploaded', this)">Cloud Synced</button>
                    <button class="filter-btn" onclick="setStatusFilter('downloaded', this)">Waiting Upload</button>
                    <button class="filter-btn" onclick="setStatusFilter('discovered', this)">On Dashcam</button>
                </div>
                <button class="filter-btn" style="margin-left:auto;" onclick="triggerBackfill()">Sync Drive Links</button>
            </div>
        </div>

        <!-- Selection Toolbar -->
        <div style="display:flex; justify-content:space-between; align-items:center; padding:8px 12px; background:#0f172a; border:1px solid var(--border-color); border-radius:8px; margin-bottom:12px;">
            <label style="display:flex; align-items:center; gap:8px; font-size:0.85rem; font-weight:600; cursor:pointer;">
                <input type="checkbox" id="select-all-cb" onchange="toggleSelectAll(this)">
                <span>Select All (<span id="visible-count">0</span> visible)</span>
            </label>
            <button id="bulk-delete-btn" class="action-btn" style="background:var(--accent-alert); color:#fff; display:none;" onclick="openDeleteModal()">
                🗑️ Delete Selected (<span id="selected-count">0</span>)
            </button>
        </div>

        <div id="video-list">Loading videos...</div>
    </div>

    <!-- Delete Confirmation Modal -->
    <div id="delete-modal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.75); z-index:1000; justify-content:center; align-items:center;">
        <div style="background:var(--card-bg); border:1px solid var(--border-color); border-radius:12px; padding:20px; max-width:480px; width:90%; box-shadow:0 10px 25px rgba(0,0,0,0.5);">
            <h3 style="margin-bottom:12px; color:var(--accent-color);">Confirm Bulk Deletion</h3>
            <p style="font-size:0.9rem; color:var(--text-color); margin-bottom:16px;">
                You are about to delete <b id="modal-selected-count">0</b> selected video(s).
            </p>
            
            <div style="background:#0f172a; padding:12px; border-radius:8px; border:1px solid var(--border-color); margin-bottom:16px;">
                <div style="font-size:0.85rem; font-weight:bold; margin-bottom:8px; color:var(--text-muted);">Select Deletion Scope:</div>
                <label style="display:block; margin-bottom:8px; font-size:0.9rem; cursor:pointer;">
                    <input type="radio" name="delete-scope" value="local" checked> Delete Local Pi copy only
                </label>
                <label style="display:block; margin-bottom:8px; font-size:0.9rem; cursor:pointer;">
                    <input type="radio" name="delete-scope" value="cloud"> Delete Cloud / Google Drive copy only
                </label>
                <label style="display:block; font-size:0.9rem; cursor:pointer;">
                    <input type="radio" name="delete-scope" value="both"> Delete both Local Pi + Google Drive
                </label>
            </div>

            <div style="font-size:0.8rem; color:var(--text-muted); margin-bottom:16px; background:rgba(234,179,8,0.1); border-left:3px solid var(--accent-warn); padding:8px 12px; border-radius:4px;">
                ℹ️ Note: Files stored on the Vantrue Dashcam SD card will NOT be deleted.
            </div>

            <div style="display:flex; justify-content:flex-end; gap:8px;">
                <button class="filter-btn" onclick="closeDeleteModal()">Cancel</button>
                <button class="action-btn" style="background:var(--accent-alert); color:#fff;" onclick="executeBulkDelete()">Confirm Delete</button>
            </div>
        </div>
    </div>

    <script>
        let allVideos = [];
        let selectedFilenames = new Set();
        let currentCameraFilter = 'all';
        let currentStatusFilter = 'all';

        async function triggerBackfill() {
            try {
                const res = await fetch('/api/backfill', { method: 'POST' });
                const data = await res.json();
                alert(data.message || 'Drive links synced.');
                loadAll();
            } catch(e) { alert('Backfill error: ' + e); }
        }

        async function loadStatus() {
            try {
                const res = await fetch('/api/status?_t=' + Date.now(), { cache: 'no-store' });
                if (!res.ok) {
                    console.error('Status request failed with status:', res.status);
                    return;
                }
                const data = await res.json();
                
                const s = data.storage || {};
                const summaryEl = document.getElementById('storage-summary');
                if (summaryEl) {
                    summaryEl.innerHTML = 
                        `<b>${s.used_gb ?? 0} GB</b> used of <b>${s.total_gb ?? 0} GB</b> (${s.free_gb ?? 0} GB free, Min reserve: ${s.min_free_space_gb ?? 0} GB)`;
                }
                const barEl = document.getElementById('storage-bar');
                if (barEl) {
                    barEl.style.width = (s.percent_used ?? 0) + '%';
                }

                const q = data.queue || {};
                const queuePendingEl = document.getElementById('queue-pending');
                if (queuePendingEl) {
                    queuePendingEl.innerHTML = `<b>${q.pending_download_count || 0}</b> files`;
                }
                const queueBreakdownEl = document.getElementById('queue-breakdown');
                if (queueBreakdownEl) {
                    queueBreakdownEl.innerHTML = 
                        `<span class="pill pill-warn">${q.pending_event_count || 0} Event</span> <span class="pill">${q.pending_normal_count || 0} Normal</span>`;
                }

                const w0 = (data.network && data.network.wlan0) || {};
                const w0El = document.getElementById('net-wlan0');
                if (w0El) {
                    w0El.innerHTML = 
                        `${w0.ip || 'N/A'} <span class="pill ${w0.dashcam_reachable ? 'pill-green':'pill-warn'}">${w0.dashcam_reachable ? 'Dashcam Reachable':'Idle'}</span>`;
                }

                const w1 = (data.network && data.network.wlan1) || {};
                const w1El = document.getElementById('net-wlan1');
                if (w1El) {
                    w1El.innerHTML = 
                        `${w1.ip || 'N/A'} <span class="pill ${w1.internet_reachable ? 'pill-green':'pill-warn'}">${w1.internet_reachable ? 'Internet OK':'Local Only'}</span>`;
                }
            } catch(e) { console.error('Status error:', e); }
        }

        async function triggerRescan() {
            if (!confirm('Trigger a full remote camera discovery scan?')) return;
            try {
                const res = await fetch('/api/rescan', { method: 'POST' });
                const data = await res.json();
                alert(data.message || 'Rescan triggered.');
                loadAll();
            } catch(e) { alert('Rescan error: ' + e); }
        }

        async function loadVideos() {
            try {
                const res = await fetch('/api/videos?status=all&_t=' + Date.now(), { cache: 'no-store' });
                if (!res.ok) {
                    console.error('Video request failed with status:', res.status);
                    const container = document.getElementById('video-list');
                    if (container) container.innerHTML = '<div style="color:var(--accent-alert); padding:12px 0;">Error loading video library.</div>';
                    return;
                }
                const data = await res.json();
                allVideos = data.videos || [];
                renderVideos();
            } catch(e) {
                console.error('Video load error:', e);
                const container = document.getElementById('video-list');
                if (container) container.innerHTML = '<div style="color:var(--accent-alert); padding:12px 0;">Error loading video library.</div>';
            }
        }

        function setCameraFilter(camera, btn) {
            currentCameraFilter = camera;
            document.querySelectorAll('.filter-controls-camera .filter-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            renderVideos();
        }

        function setStatusFilter(status, btn) {
            currentStatusFilter = status;
            document.querySelectorAll('.filter-controls-status .filter-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            renderVideos();
        }

        function getFilteredVideos() {
            return allVideos.filter(v => {
                const matchesCamera = (currentCameraFilter === 'all' || v.category_position === currentCameraFilter);
                let matchesStatus = true;
                if (currentStatusFilter === 'uploaded') {
                    matchesStatus = (v.file_state === 'cloud' || v.file_state === 'local+cloud' || v.status === 'uploaded');
                } else if (currentStatusFilter === 'downloaded') {
                    matchesStatus = (v.file_state === 'local' || v.status === 'downloaded');
                } else if (currentStatusFilter === 'discovered') {
                    matchesStatus = (v.file_state === 'dashcam' || v.status === 'discovered');
                }
                return matchesCamera && matchesStatus;
            });
        }

        function renderVideos() {
            const visibleVideos = getFilteredVideos();
            document.getElementById('visible-count').innerText = visibleVideos.length;
            
            const selectAllCb = document.getElementById('select-all-cb');
            if (visibleVideos.length > 0) {
                selectAllCb.checked = visibleVideos.every(v => selectedFilenames.has(v.filename));
            } else {
                selectAllCb.checked = false;
            }

            updateSelectionUI();

            const container = document.getElementById('video-list');
            if (visibleVideos.length === 0) {
                container.innerHTML = '<div style="color:var(--text-muted); padding:12px 0;">No videos match selected filter.</div>';
                return;
            }

            container.innerHTML = visibleVideos.map(v => {
                const isChecked = selectedFilenames.has(v.filename) ? 'checked' : '';
                const hasCloudUrl = Boolean(v.cloud_play_url);
                const localStreamUrl = `/stream/${encodeURIComponent(v.filename)}?source=local`;
                const cloudStreamUrl = `/stream/${encodeURIComponent(v.filename)}?source=cloud`;

                let stateBadge = '';
                let stateClass = '';
                let actionHtml = '';

                switch (v.file_state) {
                    case 'dashcam':
                        stateBadge = '📷 On Dashcam';
                        stateClass = 'pill-info';
                        actionHtml = `<span style="font-size:0.85rem; color:var(--accent-color)">📷 On Dashcam (Awaiting Pi download)</span>`;
                        break;
                    case 'local':
                        stateBadge = '💾 Local (Pi)';
                        stateClass = 'pill-warn';
                        actionHtml = `<button class="action-btn local-btn" onclick="playVideo(this, '${localStreamUrl}')">▶️ Play Local (Pi)</button>`;
                        break;
                    case 'local+cloud':
                        stateBadge = '⚡ Local + Cloud';
                        stateClass = 'pill-green';
                        actionHtml = `<button class="action-btn local-btn" onclick="playVideo(this, '${localStreamUrl}')">▶️ Play Local (Pi)</button>`;
                        actionHtml += `<button class="action-btn cloud-btn" onclick="playVideo(this, '${cloudStreamUrl}')">☁️ Play Cloud (Proxy)</button>`;
                        if (hasCloudUrl) {
                            actionHtml += `<a href="${v.cloud_play_url}" target="_blank" class="action-btn link-btn">🔗 Drive Tab</a>`;
                        }
                        break;
                    case 'cloud':
                        stateBadge = '☁️ Cloud Only';
                        stateClass = 'pill-green';
                        actionHtml = `<button class="action-btn cloud-btn" onclick="playVideo(this, '${cloudStreamUrl}')">☁️ Play Cloud (Proxy)</button>`;
                        if (hasCloudUrl) {
                            actionHtml += `<a href="${v.cloud_play_url}" target="_blank" class="action-btn link-btn">🔗 Drive Tab</a>`;
                        }
                        break;
                    case 'missing':
                        stateBadge = '⚠️ Missing Local File';
                        stateClass = 'pill-alert';
                        actionHtml = `<span style="font-size:0.85rem; color:var(--accent-alert)">⚠️ Local file missing unexpectedly on Pi</span>`;
                        break;
                    case 'purged':
                        stateBadge = '🗑️ Local Purged';
                        stateClass = 'pill-alert';
                        actionHtml = `<span style="font-size:0.85rem; color:var(--text-muted)">Local copy purged</span>`;
                        break;
                    default:
                        stateBadge = v.file_state || v.status;
                        stateClass = 'pill-warn';
                        actionHtml = `<span style="font-size:0.85rem; color:var(--text-muted)">Status: ${v.status}</span>`;
                }

                return `
                <div class="video-item">
                    <div class="video-header">
                        <label style="display:flex; align-items:center; gap:8px; cursor:pointer;">
                            <input type="checkbox" class="video-cb" ${isChecked} onchange="toggleSelectVideo('${v.filename}', this.checked)">
                            <span class="video-title">${v.filename}</span>
                        </label>
                        <span class="pill ${stateClass}">${stateBadge}</span>
                    </div>
                    <div class="video-meta">
                        <span>📅 ${v.recording_timestamp}</span>
                        <span>🏷️ ${v.category}</span>
                        <span>💾 ${v.file_size_mb} MB</span>
                    </div>
                    <div style="margin-top:8px; display:flex; flex-wrap:wrap; gap:6px; align-items:center;">
                        ${actionHtml}
                    </div>
                </div>
            `}).join('');
        }

        function toggleSelectVideo(filename, isSelected) {
            if (isSelected) {
                selectedFilenames.add(filename);
            } else {
                selectedFilenames.delete(filename);
            }
            const visibleVideos = getFilteredVideos();
            const selectAllCb = document.getElementById('select-all-cb');
            if (visibleVideos.length > 0) {
                selectAllCb.checked = visibleVideos.every(v => selectedFilenames.has(v.filename));
            }
            updateSelectionUI();
        }

        function toggleSelectAll(cb) {
            const visibleVideos = getFilteredVideos();
            visibleVideos.forEach(v => {
                if (cb.checked) {
                    selectedFilenames.add(v.filename);
                } else {
                    selectedFilenames.delete(v.filename);
                }
            });
            renderVideos();
        }

        function updateSelectionUI() {
            const count = selectedFilenames.size;
            document.getElementById('selected-count').innerText = count;
            const btn = document.getElementById('bulk-delete-btn');
            if (count > 0) {
                btn.style.display = 'inline-block';
            } else {
                btn.style.display = 'none';
            }
        }

        function openDeleteModal() {
            if (selectedFilenames.size === 0) return;
            document.getElementById('modal-selected-count').innerText = selectedFilenames.size;
            const modal = document.getElementById('delete-modal');
            modal.style.display = 'flex';
        }

        function closeDeleteModal() {
            const modal = document.getElementById('delete-modal');
            modal.style.display = 'none';
        }

        async function executeBulkDelete() {
            const filenames = Array.from(selectedFilenames);
            if (filenames.length === 0) return;

            const scopeRadios = document.getElementsByName('delete-scope');
            let scope = 'local';
            for (const r of scopeRadios) {
                if (r.checked) { scope = r.value; break; }
            }

            closeDeleteModal();

            try {
                const res = await fetch('/api/videos/delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ filenames: filenames, scope: scope })
                });
                const data = await res.json();
                
                let summaryMsg = `Bulk deletion finished (Scope: ${scope}):\n` +
                    `Total: ${data.total}, Successful: ${data.successful}, Failed: ${data.failed}\n\n`;
                if (data.results && data.results.length > 0) {
                    summaryMsg += data.results.map(r => `• ${r.filename}: ${r.message}`).join('\\n');
                }
                alert(summaryMsg);

                selectedFilenames.clear();
                loadAll();
            } catch(e) {
                alert('Bulk deletion request error: ' + e);
            }
        }

        function playVideo(btn, streamUrl) {
            const actionContainer = btn.parentElement;
            const itemContainer = actionContainer.parentElement;
            const existingMedia = itemContainer.querySelector('video');

            if (existingMedia) {
                const currentSrc = existingMedia.getAttribute('data-stream-url');
                existingMedia.remove();
                actionContainer.querySelectorAll('button').forEach(b => {
                    const orig = b.getAttribute('data-original-label');
                    if (orig) b.innerText = orig;
                });
                if (currentSrc === streamUrl) {
                    return;
                }
            }

            if (!btn.getAttribute('data-original-label')) {
                btn.setAttribute('data-original-label', btn.innerText);
            }

            const video = document.createElement('video');
            video.controls = true;
            video.preload = 'metadata';
            video.src = streamUrl;
            video.setAttribute('data-stream-url', streamUrl);
            video.style.width = '100%';
            video.style.maxHeight = '360px';
            video.style.borderRadius = '8px';
            video.style.marginTop = '8px';
            video.style.background = '#000';

            itemContainer.appendChild(video);
            video.play().catch(e => console.log('Autoplay deferred:', e));
            btn.innerText = '⏹ Stop Playing';
        }

        function setFilter(filter, btn) {
            currentFilter = filter;
            document.querySelectorAll('.filter-controls .filter-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            loadVideos();
        }

        async function triggerCleanup() {
            if (!confirm('Run rolling storage cleanup of oldest uploaded videos?')) return;
            try {
                const res = await fetch('/api/cleanup', { method: 'POST' });
                const data = await res.json();
                alert(`Cleanup completed. Available space: ${data.free_gb} GB`);
                loadAll();
            } catch(e) { alert('Cleanup error: ' + e); }
        }

        async function loadAll() {
            console.log("[dashboard] VANTRUE DASHBOARD BUILD 2026-09-05");
            console.log("[dashboard] START loadAll");
            
            try {
                console.log("[dashboard] loading status...");
                await loadStatus();
                console.log("[status] PASS");
            } catch (err) {
                console.error("[status] FAIL", err);
            }

            try {
                console.log("[dashboard] loading videos...");
                await loadVideos();
                console.log("[videos] PASS");
            } catch (err) {
                console.error("[videos] FAIL", err);
            }
        }

        if (document.readyState === 'complete' || document.readyState === 'interactive') {
            setTimeout(loadAll, 1);
        } else {
            document.addEventListener('DOMContentLoaded', loadAll);
        }
        window.addEventListener('load', loadAll);
    </script>
</body>
</html>
"""


def main():
    setup_logging()
    host = Config.WEB_HOST
    port = Config.WEB_PORT

    server = ThreadedHTTPServer((host, port), VantrueWebHandler)
    logger.info(f"Vantrue Pi Web Dashboard listening on {host}:{port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Web server shutting down.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
