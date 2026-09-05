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
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status in (200, 204)
    except Exception:
        return False


def check_dashcam_quick() -> bool:
    """Quick non-blocking dashcam HTTP endpoint reachability probe."""
    try:
        req = urllib.request.Request(
            Config.VANTRUE_BASE_URL,
            headers={"User-Agent": "VantruePiAutomation/1.0"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
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
    """HTTP Request Handler for Vantrue Pi Web Dashboard and Video Byte-Range Streaming."""

    def log_message(self, format_str: str, *args: float):
        """Override log_message for unified python logging."""
        logger.debug(f"{self.address_string()} - {format_str % args}")

    def _send_json(self, status_code: int, data: Dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
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
            self._handle_stream_video(filename)
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
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Endpoint not found")

    def _handle_serve_dashboard(self):
        html_content = HTML_TEMPLATE.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html_content)))
        self.end_headers()
        self.wfile.write(html_content)

    def _handle_api_status(self):
        db = SyncDB(Config.DB_PATH)
        db.sync_physical_files(Config.LOCAL_DOWNLOAD_DIR)
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
                "local_count": stats["local_count"],
                "local_size_mb": round(stats["local_size"] / (1024 * 1024), 1),
                "uploaded_count": stats["uploaded_count"],
                "pending_upload_count": stats["pending_upload_count"],
                "total_discovered": stats["total_discovered"],
            },
            "network": {
                "wlan0": {
                    "interface": Config.VANTRUE_INTERFACE,
                    "status": wlan0_info["status"],
                    "ip": wlan0_info["ip"],
                    "dashcam_reachable": check_dashcam_quick(),
                },
                "wlan1": {
                    "interface": Config.INTERNET_INTERFACE,
                    "status": wlan1_info["status"],
                    "ip": wlan1_info["ip"],
                    "internet_reachable": check_internet_quick(),
                },
            },
            "last_uploaded_at": stats["last_uploaded_at"],
        }
        self._send_json(HTTPStatus.OK, status_data)

    def _handle_api_videos(self, query_str: str):
        params = urllib.parse.parse_qs(query_str)
        filter_status = params.get("status", ["all"])[0]
        sort_order = params.get("sort", ["desc"])[0]

        db = SyncDB(Config.DB_PATH)
        db.sync_physical_files(Config.LOCAL_DOWNLOAD_DIR)
        records = db.get_all_recordings(
            filter_status=filter_status, sort_desc=(sort_order == "desc")
        )


        base_dir = Config.LOCAL_DOWNLOAD_DIR.resolve()
        video_list = []
        for r in records:
            fn = r["filename"]
            local_path = base_dir / fn
            is_present = local_path.exists() and local_path.is_file()

            drive_id = r["drive_file_id"] if ("drive_file_id" in r.keys() and r["drive_file_id"]) else None
            cloud_play_url = f"https://drive.google.com/file/d/{drive_id}/view" if drive_id else None
            cloud_embed_url = f"https://drive.google.com/file/d/{drive_id}/preview" if drive_id else None
            cloud_direct_url = f"https://drive.google.com/uc?export=download&id={drive_id}" if drive_id else None

            cat_info = categorize_filename(fn)
            video_list.append(
                {
                    "filename": fn,
                    "recording_timestamp": r["recording_timestamp"],
                    "file_size": r["file_size"],
                    "file_size_mb": round(r["file_size"] / (1024 * 1024), 1),
                    "status": r["status"],
                    "uploaded_at": r["uploaded_at"] if "uploaded_at" in r.keys() else None,
                    "drive_file_id": drive_id,
                    "cloud_play_url": cloud_play_url,
                    "cloud_embed_url": cloud_embed_url,
                    "cloud_direct_url": cloud_direct_url,
                    "local_present": is_present,
                    "category": cat_info["label"],
                    "category_type": cat_info["type"],
                    "category_position": cat_info["position"],
                    "stream_url": f"/stream/{urllib.parse.quote(fn)}" if is_present else None,
                }
            )

        self._send_json(HTTPStatus.OK, {"videos": video_list, "count": len(video_list)})

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

    def _handle_stream_video(self, filename: str):
        """
        Stream MP4 video file supporting HTTP Byte-Range requests for iOS Safari / Chrome seeking.
        If file exists locally, stream directly from disk.
        If file is cloud-synced (uploaded/deleted), proxy stream from Google Drive via rclone cat with Byte-Range support.
        Includes strict path traversal security checks.
        """
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
        if target_path.exists() and target_path.is_file() and base_dir in target_path.parents:
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
        .pill-green { background: rgba(34, 197, 94, 0.2); color: var(--accent-green); }
        .pill-warn { background: rgba(234, 179, 8, 0.2); color: var(--accent-warn); }
        
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
            margin-top: 8px;
            background: var(--accent-color);
            color: #0f172a;
            border: none;
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 0.85rem;
            font-weight: bold;
            cursor: pointer;
        }
        .cloud-btn {
            background: var(--accent-green);
            color: #0f172a;
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
        <div class="filter-controls">
            <button class="filter-btn active" onclick="setFilter('all', this)">All</button>
            <button class="filter-btn" onclick="setFilter('uploaded', this)">Cloud Synced</button>
            <button class="filter-btn" onclick="setFilter('downloaded', this)">Waiting Upload</button>
            <button class="filter-btn" style="margin-left:auto;" onclick="triggerBackfill()">Sync Drive Links</button>
        </div>
        <div id="video-list">Loading videos...</div>
    </div>

    <script>
        let currentFilter = 'all';

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
                const res = await fetch('/api/status');
                const data = await res.json();
                
                const s = data.storage;
                document.getElementById('storage-summary').innerHTML = 
                    `<b>${s.used_gb} GB</b> used of <b>${s.total_gb} GB</b> (${s.free_gb} GB free, Min reserve: ${s.min_free_space_gb} GB)`;
                document.getElementById('storage-bar').style.width = s.percent_used + '%';

                const q = data.queue || {};
                document.getElementById('queue-pending').innerHTML = 
                    `<b>${q.pending_download_count || 0}</b> files`;
                document.getElementById('queue-breakdown').innerHTML = 
                    `<span class="pill pill-warn">${q.pending_event_count || 0} Event</span> <span class="pill">${q.pending_normal_count || 0} Normal</span>`;

                const w0 = data.network.wlan0;
                document.getElementById('net-wlan0').innerHTML = 
                    `${w0.ip} <span class="pill ${w0.dashcam_reachable ? 'pill-green':'pill-warn'}">${w0.dashcam_reachable ? 'Dashcam Reachable':'Idle'}</span>`;

                const w1 = data.network.wlan1;
                document.getElementById('net-wlan1').innerHTML = 
                    `${w1.ip} <span class="pill ${w1.internet_reachable ? 'pill-green':'pill-warn'}">${w1.internet_reachable ? 'Internet OK':'Local Only'}</span>`;
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
                const res = await fetch(`/api/videos?status=${currentFilter}`);
                const data = await res.json();
                const container = document.getElementById('video-list');
                
                if (!data.videos || data.videos.length === 0) {
                    container.innerHTML = '<div style="color:var(--text-muted); padding:12px 0;">No videos match filter.</div>';
                    return;
                }

                container.innerHTML = data.videos.map(v => {
                    const isSynced = (v.status === 'uploaded' || v.status === 'deleted');
                    const hasCloudUrl = Boolean(v.cloud_play_url);
                    const canStream = Boolean(v.local_present || isSynced);
                    const streamUrl = `/stream/${encodeURIComponent(v.filename)}`;

                    let actionHtml = '';

                    if (canStream) {
                        const playLabel = isSynced ? '▶️ Play Video (Cloud Proxy)' : '▶️ Play Video (Local Cache)';
                        actionHtml += `<button class="action-btn cloud-btn" onclick="playVideo(this, '${streamUrl}')">${playLabel}</button>`;
                    }

                    if (isSynced && hasCloudUrl) {
                        actionHtml += `<a href="${v.cloud_play_url}" target="_blank" class="action-btn" style="background:#334155; color:var(--text-color); text-decoration:none; display:inline-block; margin-left:6px;">🔗 Open Drive Tab</a>`;
                    }

                    if (!canStream && !hasCloudUrl) {
                        actionHtml = `<span style="font-size:0.8rem; color:var(--text-muted)">File purged locally (Cloud link pending)</span>`;
                    }

                    return `
                    <div class="video-item">
                        <div class="video-header">
                            <span class="video-title">${v.filename}</span>
                            <span class="pill ${isSynced ? 'pill-green':'pill-warn'}">${isSynced ? 'Synced':'Waiting Upload'}</span>
                        </div>
                        <div class="video-meta">
                            <span>📅 ${v.recording_timestamp}</span>
                            <span>🏷️ ${v.category}</span>
                            <span>💾 ${v.file_size_mb} MB</span>
                        </div>
                        <div style="margin-top:8px;">
                            ${actionHtml}
                        </div>
                    </div>
                `}).join('');
            } catch(e) { console.error('Video load error:', e); }
        }

        function playVideo(btn, streamUrl) {
            const parent = btn.parentElement;
            const existingMedia = parent.querySelector('video');
            if (existingMedia) {
                existingMedia.remove();
                const orig = btn.getAttribute('data-original-label');
                if (orig) btn.innerText = orig;
                return;
            }

            if (!btn.getAttribute('data-original-label')) {
                btn.setAttribute('data-original-label', btn.innerText);
            }

            const video = document.createElement('video');
            video.controls = true;
            video.preload = 'metadata';
            video.src = streamUrl;
            video.style.width = '100%';
            video.style.maxHeight = '360px';
            video.style.borderRadius = '8px';
            video.style.marginTop = '8px';
            video.style.background = '#000';

            parent.appendChild(video);
            video.play().catch(e => console.log('Autoplay deferred:', e));
            btn.innerText = 'Close Video Player';
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

        function loadAll() {
            loadStatus();
            loadVideos();
        }

        window.onload = loadAll;
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
