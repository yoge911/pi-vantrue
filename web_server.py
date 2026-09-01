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

            cat_info = categorize_filename(fn)
            video_list.append(
                {
                    "filename": fn,
                    "recording_timestamp": r["recording_timestamp"],
                    "file_size": r["file_size"],
                    "file_size_mb": round(r["file_size"] / (1024 * 1024), 1),
                    "status": r["status"],
                    "uploaded_at": r["uploaded_at"] if "uploaded_at" in r.keys() else None,
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

    def _handle_stream_video(self, filename: str):
        """
        Stream local MP4 video file supporting HTTP Byte-Range requests for iOS Safari / Chrome seeking.
        Includes strict path traversal security checks.
        """
        # Strict Path Traversal Guard
        base_dir = Config.LOCAL_DOWNLOAD_DIR.resolve()
        target_path = (base_dir / filename).resolve()

        if not str(target_path).startswith(str(base_dir)):
            logger.warning(f"Security Alert: Path traversal attempt blocked for '{filename}'.")
            self.send_error(HTTPStatus.FORBIDDEN, "Access denied")
            return

        if not target_path.exists() or not target_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return

        file_size = target_path.stat().st_size
        content_type = "video/mp4"
        if filename.lower().endswith(".mov"):
            content_type = "video/quicktime"

        range_header = self.headers.get("Range")

        if range_header and range_header.startswith("bytes="):
            # Parse Range Header (e.g. bytes=1000-2000 or bytes=1000-)
            try:
                byte_range = range_header.split("=")[1].strip()
                parts = byte_range.split("-")
                start_byte = int(parts[0]) if parts[0] else 0
                end_byte = int(parts[1]) if parts[1] else file_size - 1

                if start_byte >= file_size or end_byte >= file_size or start_byte > end_byte:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.end_headers()
                    return

                chunk_length = end_byte - start_byte + 1

                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Type", content_type)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start_byte}-{end_byte}/{file_size}")
                self.send_header("Content-Length", str(chunk_length))
                self.end_headers()

                with open(target_path, "rb") as f:
                    f.seek(start_byte)
                    remaining = chunk_length
                    buffer_size = 64 * 1024
                    while remaining > 0:
                        read_bytes = min(buffer_size, remaining)
                        data = f.read(read_bytes)
                        if not data:
                            break
                        self.wfile.write(data)
                        remaining -= len(data)
                return

            except (ValueError, IndexError, OSError) as exc:
                logger.debug(f"Range parsing or socket error for '{filename}': {exc}")
                return

        # Fallback: Serve full file HTTP 200
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(file_size))
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
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
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
            height: 12px;
            border-radius: 6px;
            overflow: hidden;
            margin: 10px 0;
        }
        .progress-bar-fill {
            background: var(--accent-color);
            height: 100%;
            width: 0%;
            transition: width 0.3s ease;
        }
        .status-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 12px;
        }
        .stat-box {
            background: #0f172a;
            padding: 12px;
            border-radius: 8px;
        }
        .stat-label { font-size: 0.8rem; color: var(--text-muted); }
        .stat-value { font-size: 1.1rem; font-weight: bold; margin-top: 4px; }
        .pill {
            display: inline-block;
            padding: 4px 8px;
            border-radius: 12px;
            font-size: 0.75rem;
            font-weight: bold;
        }
        .pill-green { background: rgba(34, 197, 94, 0.2); color: #4ade80; }
        .pill-warn { background: rgba(234, 179, 8, 0.2); color: #fde047; }
        .pill-alert { background: rgba(239, 68, 68, 0.2); color: #f87171; }
        
        .filter-controls {
            display: flex;
            gap: 8px;
            margin-bottom: 12px;
            overflow-x: auto;
        }
        .filter-btn {
            background: #0f172a;
            color: var(--text-muted);
            border: 1px solid var(--border-color);
            padding: 6px 12px;
            border-radius: 20px;
            font-size: 0.85rem;
            cursor: pointer;
            white-space: nowrap;
        }
        .filter-btn.active {
            background: var(--accent-color);
            color: #0f172a;
            font-weight: bold;
        }
        .video-item {
            display: flex;
            flex-direction: column;
            gap: 8px;
            border-bottom: 1px solid var(--border-color);
            padding: 12px 0;
        }
        .video-item:last-child { border-bottom: none; }
        .video-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .video-title { font-size: 0.95rem; font-weight: 600; word-break: break-all; }
        .video-meta { font-size: 0.8rem; color: var(--text-muted); display: flex; gap: 12px; }
        .action-btn {
            background: var(--accent-color);
            color: #0f172a;
            border: none;
            padding: 8px 16px;
            border-radius: 6px;
            font-weight: bold;
            cursor: pointer;
            align-self: flex-start;
            margin-top: 4px;
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
        </div>
        <div id="video-list">Loading videos...</div>
    </div>

    <script>
        let currentFilter = 'all';

        async function loadStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                
                const s = data.storage;
                document.getElementById('storage-summary').innerHTML = 
                    `<b>${s.used_gb} GB</b> used of <b>${s.total_gb} GB</b> (${s.free_gb} GB free, Min reserve: ${s.min_free_space_gb} GB)`;
                document.getElementById('storage-bar').style.width = s.percent_used + '%';

                const w0 = data.network.wlan0;
                document.getElementById('net-wlan0').innerHTML = 
                    `${w0.ip} <span class="pill ${w0.dashcam_reachable ? 'pill-green':'pill-warn'}">${w0.dashcam_reachable ? 'Dashcam Reachable':'Idle'}</span>`;

                const w1 = data.network.wlan1;
                document.getElementById('net-wlan1').innerHTML = 
                    `${w1.ip} <span class="pill ${w1.internet_reachable ? 'pill-green':'pill-warn'}">${w1.internet_reachable ? 'Internet OK':'Local Only'}</span>`;
            } catch(e) { console.error('Status error:', e); }
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

                container.innerHTML = data.videos.map(v => `
                    <div class="video-item">
                        <div class="video-header">
                            <span class="video-title">${v.filename}</span>
                            <span class="pill ${v.status === 'uploaded' ? 'pill-green':'pill-warn'}">${v.status === 'uploaded' ? 'Synced':'Waiting Upload'}</span>
                        </div>
                        <div class="video-meta">
                            <span>📅 ${v.recording_timestamp}</span>
                            <span>🏷️ ${v.category}</span>
                            <span>💾 ${v.file_size_mb} MB</span>
                        </div>
                        ${v.stream_url ? `
                            <button class="action-btn" onclick="playVideo(this, '${v.stream_url}')">Play Video</button>
                        ` : '<span style="font-size:0.8rem; color:var(--accent-warn)">Not present on disk</span>'}
                    </div>
                `).join('');
            } catch(e) { console.error('Video load error:', e); }
        }

        function playVideo(btn, url) {
            const parent = btn.parentElement;
            if (parent.querySelector('video')) {
                parent.querySelector('video').remove();
                btn.innerText = 'Play Video';
                return;
            }
            const video = document.createElement('video');
            video.controls = true;
            video.preload = 'metadata';
            video.src = url;
            parent.appendChild(video);
            video.play();
            btn.innerText = 'Close Player';
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
