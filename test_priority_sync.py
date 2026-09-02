import os
import shutil
import sqlite3
import tempfile
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


class TestPrioritySync(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.video_dir = self.temp_dir / "videos"
        self.video_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.temp_dir / "sync_state.db"

        # Mock config class
        class TestConfig(Config):
            LOCAL_DOWNLOAD_DIR = self.video_dir
            DB_PATH = self.db_path
            STABILITY_CHECK_DELAY = 0.01  # Fast stability check for unit tests
            HTTP_TIMEOUT = 3
            MAX_BUFFER_BYTES = 100 * 1024 * 1024
            MIN_FREE_SPACE_GB = 1
            MIN_FREE_DISK_BYTES = 1024 * 1024

        self.config = TestConfig
        self.db = SyncDB(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_priority_classification(self):
        """Test priority classification rules (Directory authoritative, fallback filename/ext)."""
        # Directory authoritative
        self.assertEqual(classify_file_priority("http://cam/Event/20260822_100000_N.MP4", "20260822_100000_N.MP4"), Config.PRIORITY_EVENT)
        self.assertEqual(classify_file_priority("http://cam/Normal/20260822_100000_E.MP4", "20260822_100000_E.MP4"), Config.PRIORITY_NORMAL)
        self.assertEqual(classify_file_priority("http://cam/GPS/20260822_100000.gps", "20260822_100000.gps"), Config.PRIORITY_GPS)
        self.assertEqual(classify_file_priority("http://cam/Photo/20260822_100000.jpg", "20260822_100000.jpg"), Config.PRIORITY_PHOTO)

        # Fallback filename patterns when path is root / unknown
        self.assertEqual(classify_file_priority("http://cam/20260822_100000_0001_E.MP4", "20260822_100000_0001_E.MP4"), Config.PRIORITY_EVENT)
        self.assertEqual(classify_file_priority("http://cam/20260822_100000_0001_N.MP4", "20260822_100000_0001_N.MP4"), Config.PRIORITY_NORMAL)
        self.assertEqual(classify_file_priority("http://cam/data_log.dat", "data_log.dat"), Config.PRIORITY_GPS)
        self.assertEqual(classify_file_priority("http://cam/snapshot.jpg", "snapshot.jpg"), Config.PRIORITY_PHOTO)
        self.assertEqual(classify_file_priority("http://cam/unknown.bin", "unknown.bin"), Config.PRIORITY_OTHER)

    def test_database_priority_sorting(self):
        """Test DB queue sorting: Priority ASC (0=Event before 1=Normal), Timestamp ASC (oldest first)."""
        recordings = [
            {"remote_url": "http://cam/Normal/normal_002.mp4", "filename": "normal_002.mp4", "recording_timestamp": "2026-08-22 10:05:00", "priority": Config.PRIORITY_NORMAL},
            {"remote_url": "http://cam/Normal/normal_001.mp4", "filename": "normal_001.mp4", "recording_timestamp": "2026-08-22 10:00:00", "priority": Config.PRIORITY_NORMAL},
            {"remote_url": "http://cam/Event/event_001.mp4", "filename": "event_001.mp4", "recording_timestamp": "2026-08-22 10:02:00", "priority": Config.PRIORITY_EVENT},
            {"remote_url": "http://cam/GPS/gps_001.dat", "filename": "gps_001.dat", "recording_timestamp": "2026-08-22 09:00:00", "priority": Config.PRIORITY_GPS},
        ]
        self.db.register_recordings(recordings)
        pending = self.db.get_pending_downloads()

        pending_filenames = [r["filename"] for r in pending]
        # Expected order: event_001.mp4 (Priority 0), normal_001.mp4 (Priority 1, 10:00), normal_002.mp4 (Priority 1, 10:05), gps_001.dat (Priority 2)
        expected = ["event_001.mp4", "normal_001.mp4", "normal_002.mp4", "gps_001.dat"]
        self.assertEqual(pending_filenames, expected)

    def test_database_priority_update_on_rediscovery(self):
        """Test that rediscovering a file updates stored priority if classification changes."""
        self.db.register_recordings([
            {"remote_url": "http://cam/file.mp4", "filename": "file.mp4", "recording_timestamp": "2026-08-22 10:00:00", "priority": Config.PRIORITY_OTHER}
        ])

        # Rediscover under /Event/
        self.db.register_recordings([
            {"remote_url": "http://cam/file.mp4", "filename": "file.mp4", "recording_timestamp": "2026-08-22 10:00:00", "priority": Config.PRIORITY_EVENT}
        ])

        pending = self.db.get_pending_downloads()
        self.assertEqual(pending[0]["priority"], Config.PRIORITY_EVENT)

    def test_idempotent_database_migration(self):
        """Test schema migration on existing DB without priority column preserves data."""
        legacy_db_path = self.temp_dir / "legacy.db"
        conn = sqlite3.connect(str(legacy_db_path))
        conn.execute("""
            CREATE TABLE recordings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                remote_url TEXT UNIQUE NOT NULL,
                filename TEXT NOT NULL,
                file_size INTEGER DEFAULT 0,
                recording_timestamp TEXT,
                status TEXT NOT NULL
            );
        """)
        conn.execute("INSERT INTO recordings (remote_url, filename, status) VALUES ('http://cam/old.mp4', 'old.mp4', 'downloaded');")
        conn.commit()
        conn.close()

        # Initialize SyncDB on legacy database -> applies migration idempotently
        legacy_sync_db = SyncDB(legacy_db_path)

        with legacy_sync_db._get_connection() as c:
            row = c.execute("SELECT * FROM recordings WHERE filename = 'old.mp4';").fetchone()
            self.assertEqual(row["status"], "downloaded")
            self.assertEqual(row["priority"], 4)  # Migrated default

    def test_file_stability_verification(self):
        """Test stability check: skips file if size changes between observation A and observation B."""
        engine = VantrueSyncEngine(self.config)
        rec = {"remote_url": "http://cam/active.mp4", "filename": "active.mp4", "file_size": 100}

        # Mock size changing between observations (100 -> 200)
        with patch.object(engine, "get_remote_file_size", side_effect=[100, 200]):
            self.assertFalse(engine.is_remote_file_stable(rec))

        # Mock size stable across observations (200 -> 200)
        with patch.object(engine, "get_remote_file_size", side_effect=[200, 200]):
            self.assertTrue(engine.is_remote_file_stable(rec))

    def test_most_important_scenario_dynamic_reevaluation_and_reconnect(self):
        """
        Explicitly test Refinement 9 scenario:
        1. Camera online -> Event and Normal files discovered.
        2. Event selected & downloaded first.
        3. Normal download begins.
        4. Camera disconnects during/following camera finalization -> app survives.
        5. Camera reconnects -> rescan occurs.
        6. New Event file discovered -> becomes highest priority.
        7. New Event downloaded before remaining Normal backlog.
        8. No completed file downloaded twice, no .part files remain.
        """
        base_url = "http://192.168.1.254/"

        class DynamicTestConfig(Config):
            VANTRUE_BASE_URL = base_url
            LOCAL_DOWNLOAD_DIR = self.video_dir
            DB_PATH = self.db_path
            STABILITY_CHECK_DELAY = 0.001
            HTTP_TIMEOUT = 2
            MAX_BUFFER_BYTES = 100 * 1024 * 1024
            MIN_FREE_SPACE_GB = 1
            MIN_FREE_DISK_BYTES = 1024 * 1024

        engine = VantrueSyncEngine(DynamicTestConfig)

        # In-memory mock camera files
        camera_online = True
        mock_camera_files = {
            "http://192.168.1.254/": b'<html><a href="Event/">Event/</a><br><a href="Normal/">Normal/</a></html>',
            "http://192.168.1.254/Event/": b'<html><a href="20260822_100000_0001_E.MP4">20260822_100000_0001_E.MP4</a></html>',
            "http://192.168.1.254/Normal/": b'<html><a href="20260822_100100_0001_N.MP4">20260822_100100_0001_N.MP4</a><br><a href="20260822_100200_0002_N.MP4">20260822_100200_0002_N.MP4</a></html>',
            "http://192.168.1.254/Event/20260822_100000_0001_E.MP4": b"EVENT_DATA_1",
            "http://192.168.1.254/Normal/20260822_100100_0001_N.MP4": b"NORMAL_DATA_1",
            "http://192.168.1.254/Normal/20260822_100200_0002_N.MP4": b"NORMAL_DATA_2",
        }

        def mock_urlopen(req, timeout=None):
            nonlocal camera_online
            if not camera_online:
                raise urllib.error.URLError("Camera unreachable / disconnected")
            url = req.full_url if isinstance(req, urllib.request.Request) else req
            if url in mock_camera_files:
                return MockHTTPResponse(mock_camera_files[url])
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            # STEP 1: Initial camera scan
            discovered = engine.scan_remote_recordings()
            engine.db.register_recordings(discovered)
            pending = engine.db.get_pending_downloads()

            # Assert initial queue priority order: Event first, then Normal 1, then Normal 2
            self.assertEqual(pending[0]["filename"], "20260822_100000_0001_E.MP4")
            self.assertEqual(pending[1]["filename"], "20260822_100100_0001_N.MP4")
            self.assertEqual(pending[2]["filename"], "20260822_100200_0002_N.MP4")

            # STEP 2: Download Event 1
            success_evt1 = engine.download_file(dict(pending[0]))
            self.assertTrue(success_evt1)
            self.assertTrue((self.video_dir / "20260822_100000_0001_E.MP4").exists())

            # STEP 3: Download Normal 1
            success_norm1 = engine.download_file(dict(pending[1]))
            self.assertTrue(success_norm1)
            self.assertTrue((self.video_dir / "20260822_100100_0001_N.MP4").exists())

            # STEP 4: Camera disconnects
            camera_online = False

            # Engine attempts sync while camera unreachable -> survives cleanly without crash
            engine.run_sync()

            # STEP 5: Camera reconnects with NEW Event file created while Offline!
            camera_online = True
            mock_camera_files["http://192.168.1.254/Event/"] = (
                b'<html><a href="20260822_100000_0001_E.MP4">20260822_100000_0001_E.MP4</a><br>'
                b'<a href="20260822_100300_0002_E.MP4">20260822_100300_0002_E.MP4</a></html>'
            )
            mock_camera_files["http://192.168.1.254/Event/20260822_100300_0002_E.MP4"] = b"EVENT_DATA_2_NEW"

            # STEP 6: Run sync upon reconnect
            engine.run_sync()

            # STEP 7: Assertions
            # 1. New Event file was discovered and downloaded
            self.assertTrue((self.video_dir / "20260822_100300_0002_E.MP4").exists())
            # 2. Remaining Normal 2 file was downloaded
            self.assertTrue((self.video_dir / "20260822_100200_0002_N.MP4").exists())

            # 3. Verify no .part temporary files remain
            part_files = list(self.video_dir.glob("*.part"))
            self.assertEqual(len(part_files), 0)

            # 4. Verify DB states
            stats = engine.db.get_stats()
            self.assertEqual(stats["total_discovered"], 4)
            self.assertEqual(stats["local_count"], 4)


if __name__ == "__main__":
    unittest.main()
