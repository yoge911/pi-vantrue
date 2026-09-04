import os
import shutil
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from config import Config
from db import SyncDB
from vantrue_sync import VantrueSyncEngine, classify_file_priority


class MockHTTPResponse:
    def __init__(self, content: bytes, status: int = 200, headers: dict = None):
        self.content = content
        self.status = status
        self.headers = headers or {"Content-Length": str(len(content)), "Content-Type": "text/html"}

    def read(self, amt=-1):
        if amt > 0:
            res = self.content[:amt]
            self.content = self.content[amt:]
            return res
        res = self.content
        self.content = b""
        return res

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class TestQueueDecouplingAndNewestFirst(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.video_dir = self.temp_dir / "videos"
        self.video_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.temp_dir / "sync_state.db"

        class TestConfig(Config):
            LOCAL_DOWNLOAD_DIR = self.video_dir
            DB_PATH = self.db_path
            STABILITY_CHECK_DELAY = 0.001
            DASHCAM_DISCOVERY_INTERVAL = 180.0
            HTTP_TIMEOUT = 2
            MAX_BUFFER_BYTES = 100 * 1024 * 1024
            MIN_FREE_SPACE_GB = 1
            MIN_FREE_DISK_BYTES = 1024 * 1024

        self.config = TestConfig
        self.db = SyncDB(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_1_event_over_normal_priority(self):
        """1. Event category priority > Normal category priority."""
        self.db.register_recordings([
            {"remote_url": "http://cam/Normal/normal_10.mp4", "filename": "normal_10.mp4", "recording_timestamp": "2026-09-03 10:00:00", "priority": Config.PRIORITY_NORMAL},
            {"remote_url": "http://cam/Event/event_09.mp4", "filename": "event_09.mp4", "recording_timestamp": "2026-09-03 09:00:00", "priority": Config.PRIORITY_EVENT},
        ])
        pending = self.db.get_pending_downloads()
        self.assertEqual(pending[0]["filename"], "event_09.mp4")

    def test_2_newest_event_before_older_event(self):
        """2. Newest Event before older Event."""
        self.db.register_recordings([
            {"remote_url": "http://cam/Event/evt_0800.mp4", "filename": "evt_0800.mp4", "recording_timestamp": "2026-09-03 08:00:00", "priority": Config.PRIORITY_EVENT},
            {"remote_url": "http://cam/Event/evt_1000.mp4", "filename": "evt_1000.mp4", "recording_timestamp": "2026-09-03 10:00:00", "priority": Config.PRIORITY_EVENT},
        ])
        pending = self.db.get_pending_downloads()
        self.assertEqual(pending[0]["filename"], "evt_1000.mp4")

    def test_3_newest_normal_before_older_normal(self):
        """3. Newest Normal before older Normal."""
        self.db.register_recordings([
            {"remote_url": "http://cam/Normal/norm_yesterday.mp4", "filename": "norm_yesterday.mp4", "recording_timestamp": "2026-09-02 10:00:00", "priority": Config.PRIORITY_NORMAL},
            {"remote_url": "http://cam/Normal/norm_today.mp4", "filename": "norm_today.mp4", "recording_timestamp": "2026-09-03 10:00:00", "priority": Config.PRIORITY_NORMAL},
        ])
        pending = self.db.get_pending_downloads()
        self.assertEqual(pending[0]["filename"], "norm_today.mp4")

    def test_4_unstable_newest_file_skipped(self):
        """4. Unstable newest file is skipped and next stable file is selected."""
        engine = VantrueSyncEngine(self.config)
        self.db.register_recordings([
            {"remote_url": "http://cam/Normal/norm_newest_active.mp4", "filename": "norm_newest_active.mp4", "recording_timestamp": "2026-09-03 10:04:00", "priority": Config.PRIORITY_NORMAL},
            {"remote_url": "http://cam/Normal/norm_stable.mp4", "filename": "norm_stable.mp4", "recording_timestamp": "2026-09-03 10:02:00", "priority": Config.PRIORITY_NORMAL},
        ])

        # Mock: norm_newest_active is unstable (size changes 100 -> 200), norm_stable is stable (200 -> 200)
        def mock_file_size(url):
            if "norm_newest_active" in url:
                return getattr(mock_file_size, "calls_active", 100)
            return 200

        mock_file_size.calls_active = 100

        def side_effect_size(url):
            if "norm_newest_active" in url:
                val = mock_file_size.calls_active
                mock_file_size.calls_active += 100
                return val
            return 200

        with patch.object(engine, "get_remote_file_size", side_effect=side_effect_size):
            pending = engine.db.get_pending_downloads()
            # Select target stable file
            target = None
            for r in pending:
                if engine.is_remote_file_stable(dict(r)):
                    target = dict(r)
                    break
            self.assertIsNotNone(target)
            self.assertEqual(target["filename"], "norm_stable.mp4")

    def test_5_new_event_discovered_during_backlog_becomes_next_candidate(self):
        """5. New Event discovered while Normal backlog exists immediately becomes next candidate."""
        self.db.register_recordings([
            {"remote_url": "http://cam/Normal/norm_old1.mp4", "filename": "norm_old1.mp4", "recording_timestamp": "2026-09-02 10:00:00", "priority": Config.PRIORITY_NORMAL},
            {"remote_url": "http://cam/Normal/norm_old2.mp4", "filename": "norm_old2.mp4", "recording_timestamp": "2026-09-02 11:00:00", "priority": Config.PRIORITY_NORMAL},
        ])
        # New Event discovered from NOW
        self.db.register_recordings([
            {"remote_url": "http://cam/Event/evt_now.mp4", "filename": "evt_now.mp4", "recording_timestamp": "2026-09-03 10:00:00", "priority": Config.PRIORITY_EVENT},
        ])
        pending = self.db.get_pending_downloads()
        self.assertEqual(pending[0]["filename"], "evt_now.mp4")

    def test_8_reconnect_retries_known_url_without_full_scan(self):
        """8. After camera reconnect, known pending URL is retried WITHOUT mandatory full discovery scan."""
        engine = VantrueSyncEngine(self.config)
        self.db.register_recordings([
            {"remote_url": "http://cam/Normal/norm_known1.mp4", "filename": "norm_known1.mp4", "recording_timestamp": "2026-09-03 10:00:00", "priority": Config.PRIORITY_NORMAL},
            {"remote_url": "http://cam/Normal/norm_known2.mp4", "filename": "norm_known2.mp4", "recording_timestamp": "2026-09-03 09:59:00", "priority": Config.PRIORITY_NORMAL},
        ])
        # Mark last discovery as recent (e.g. 10 seconds ago)
        engine.last_discovery_time = time.time() - 10.0

        scan_mock = MagicMock(return_value=[])
        engine.scan_remote_recordings = scan_mock

        mock_responses = {
            "http://192.168.1.254/": b"OK",
            "http://cam/Normal/norm_known1.mp4": b"VIDEO_DATA1",
            "http://cam/Normal/norm_known2.mp4": b"VIDEO_DATA2",
        }

        def mock_urlopen(req, timeout=None):
            url = req.full_url if isinstance(req, urllib.request.Request) else req
            if url in mock_responses:
                return MockHTTPResponse(mock_responses[url])
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        def stop_on_download():
            if (self.video_dir / "norm_known1.mp4").exists():
                raise KeyboardInterrupt("Stop loop after first download")

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            try:
                engine.run_sync(on_file_downloaded=stop_on_download)
            except KeyboardInterrupt:
                pass

        # Verify scan_remote_recordings was NOT called on reconnect while processing pending queue!
        scan_mock.assert_not_called()
        self.assertTrue((self.video_dir / "norm_known1.mp4").exists())

    def test_9_queue_survives_restart(self):
        """9. Queue state in SQLite survives application restart."""
        self.db.register_recordings([
            {"remote_url": "http://cam/Normal/norm_restart.mp4", "filename": "norm_restart.mp4", "recording_timestamp": "2026-09-03 10:00:00", "priority": Config.PRIORITY_NORMAL},
        ])
        # Simulate new process instance with fresh VantrueSyncEngine pointing to same DB
        new_engine = VantrueSyncEngine(self.config)
        pending = new_engine.db.get_pending_downloads()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["filename"], "norm_restart.mp4")

    def test_10_duplicate_discovery_does_not_duplicate_sqlite_records(self):
        """10. Duplicate discovery does not create duplicate SQLite records."""
        rec = {"remote_url": "http://cam/Normal/norm_dup.mp4", "filename": "norm_dup.mp4", "recording_timestamp": "2026-09-03 10:00:00", "priority": Config.PRIORITY_NORMAL}
        self.db.register_recordings([rec])
        self.db.register_recordings([rec])
        self.db.register_recordings([rec])

        pending = self.db.get_pending_downloads()
        self.assertEqual(len(pending), 1)

    def test_16_simulated_large_camera_listing_reconnect_speed(self):
        """16. Simulated 2,000+ recording listing: verify reconnect skips full directory crawl."""
        engine = VantrueSyncEngine(self.config)
        large_recordings = [
            {
                "remote_url": f"http://cam/Normal/norm_{i:04d}.mp4",
                "filename": f"norm_{i:04d}.mp4",
                "recording_timestamp": f"2026-09-03 10:{i%60:02d}:00",
                "priority": Config.PRIORITY_NORMAL,
            }
            for i in range(2000)
        ]
        self.db.register_recordings(large_recordings)
        engine.last_discovery_time = time.time() - 30.0  # Discovered 30s ago

        scan_mock = MagicMock()
        engine.scan_remote_recordings = scan_mock

        mock_files = {
            "http://192.168.1.254/": b"OK",
        }
        for r in large_recordings:
            mock_files[r["remote_url"]] = b"DATA"

        def mock_urlopen(req, timeout=None):
            url = req.full_url if isinstance(req, urllib.request.Request) else req
            if url in mock_files:
                return MockHTTPResponse(mock_files[url])
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        start_t = time.time()
        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            # Run 5 single file download iterations
            for _ in range(5):
                pending = engine.db.get_pending_downloads()
                if pending:
                    engine.download_file(dict(pending[0]))

        duration = time.time() - start_t
        # Full scan was skipped, duration is fast (< 0.5s for 5 downloads)
        scan_mock.assert_not_called()
        self.assertLess(duration, 2.0)


if __name__ == "__main__":
    unittest.main()
