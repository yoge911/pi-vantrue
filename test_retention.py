import json
import shutil
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

from config import Config
from db import SyncDB
from retention import RetentionManager
from web_server import VantrueWebHandler, ThreadedHTTPServer


class TestRetentionAndWeb(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.video_dir = self.temp_dir / "videos"
        self.video_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.temp_dir / "test_state.db"

        # Mock config instance
        self.mock_config = MagicMock()
        self.mock_config.LOCAL_DOWNLOAD_DIR = self.video_dir
        self.mock_config.DB_PATH = self.db_path
        self.mock_config.MIN_FREE_SPACE_GB = 15
        self.mock_config.MIN_FREE_DISK_BYTES = 15 * 1024 * 1024 * 1024

        self.db = SyncDB(self.db_path)
        self.retention_mgr = RetentionManager(self.mock_config)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_free_space_above_threshold_nothing_deleted(self):
        """Case 1: Free space above threshold -> no cleanup required."""
        with patch.object(self.retention_mgr, "get_free_space_bytes", return_value=20 * 1024 ** 3):
            safe, code = self.retention_mgr.cleanup_uploaded_files_if_needed()
            self.assertTrue(safe)
            self.assertEqual(code, "ok")

    def test_cleanup_deletes_oldest_uploaded_first(self):
        """Case 2: Free space below threshold -> oldest uploaded videos deleted first."""
        # Create 2 uploaded files
        f1 = self.video_dir / "20260801_100000.mp4"
        f2 = self.video_dir / "20260802_100000.mp4"
        f1.write_bytes(b"A" * 1024 * 1024)
        f2.write_bytes(b"B" * 1024 * 1024)

        self.db.register_recordings([
            {"remote_url": "http://cam/f1.mp4", "filename": "20260801_100000.mp4", "recording_timestamp": "2026-08-01 10:00:00"},
            {"remote_url": "http://cam/f2.mp4", "filename": "20260802_100000.mp4", "recording_timestamp": "2026-08-02 10:00:00"},
        ])
        self.db.mark_downloaded("http://cam/f1.mp4", 1024 * 1024)
        self.db.mark_downloaded("http://cam/f2.mp4", 1024 * 1024)
        self.db.mark_uploaded("http://cam/f1.mp4")
        self.db.mark_uploaded("http://cam/f2.mp4")

        # Mock free space low initially (10GB), then restored after 1 deletion (16GB)
        free_space_mock = [10 * 1024 ** 3, 16 * 1024 ** 3]
        with patch.object(self.retention_mgr, "get_free_space_bytes", side_effect=free_space_mock):
            safe, code = self.retention_mgr.cleanup_uploaded_files_if_needed()
            self.assertTrue(safe)
            self.assertEqual(code, "cleanup_restored")

            # f1 (oldest uploaded) should be deleted
            self.assertFalse(f1.exists())
            # f2 (newer uploaded) should remain
            self.assertTrue(f2.exists())

    def test_unsynced_files_skipped(self):
        """Case 3 & 4: Unsynced files (status='downloaded') are skipped, only uploaded deleted."""
        f_unsynced = self.video_dir / "20260801_old_unsynced.mp4"
        f_uploaded = self.video_dir / "20260802_newer_uploaded.mp4"
        f_unsynced.write_bytes(b"X" * 1024)
        f_uploaded.write_bytes(b"Y" * 1024)

        self.db.register_recordings([
            {"remote_url": "http://cam/unsynced.mp4", "filename": "20260801_old_unsynced.mp4", "recording_timestamp": "2026-08-01 10:00:00"},
            {"remote_url": "http://cam/uploaded.mp4", "filename": "20260802_newer_uploaded.mp4", "recording_timestamp": "2026-08-02 10:00:00"},
        ])
        self.db.mark_downloaded("http://cam/unsynced.mp4", 1024)
        self.db.mark_downloaded("http://cam/uploaded.mp4", 1024)
        self.db.mark_uploaded("http://cam/uploaded.mp4")

        # Mock free space low initially (10GB), then restored after 1 deletion (16GB)
        free_space_mock = [10 * 1024 ** 3, 16 * 1024 ** 3]
        with patch.object(self.retention_mgr, "get_free_space_bytes", side_effect=free_space_mock):
            safe, code = self.retention_mgr.cleanup_uploaded_files_if_needed()
            self.assertTrue(safe)
            self.assertFalse(f_uploaded.exists())
            self.assertTrue(f_unsynced.exists())  # Unsynced file MUST BE PRESERVED!

    def test_no_uploaded_files_remain_reports_low_space(self):
        """Case 6: No uploaded files remain -> unsynced files untouched, reports low space condition."""
        f_unsynced = self.video_dir / "unsynced.mp4"
        f_unsynced.write_bytes(b"Z" * 1024)

        self.db.register_recordings([
            {"remote_url": "http://cam/unsynced.mp4", "filename": "unsynced.mp4", "recording_timestamp": "2026-08-01 10:00:00"},
        ])
        self.db.mark_downloaded("http://cam/unsynced.mp4", 1024)

        with patch.object(self.retention_mgr, "get_free_space_bytes", return_value=10 * 1024 ** 3):
            safe, code = self.retention_mgr.cleanup_uploaded_files_if_needed()
            self.assertFalse(safe)
            self.assertEqual(code, "no_uploaded_files_remain")
            self.assertTrue(f_unsynced.exists())  # MUST NOT BE DELETED

    def test_missing_local_file_reconciled_gracefully(self):
        """Case 7: DB references uploaded file that was missing on disk -> reconciled without crashing."""
        self.db.register_recordings([
            {"remote_url": "http://cam/missing.mp4", "filename": "missing.mp4", "recording_timestamp": "2026-08-01 10:00:00"},
        ])
        self.db.mark_downloaded("http://cam/missing.mp4", 1024)
        self.db.mark_uploaded("http://cam/missing.mp4")

        free_space_mock = [10 * 1024 ** 3, 10 * 1024 ** 3]
        with patch.object(self.retention_mgr, "get_free_space_bytes", side_effect=free_space_mock):
            safe, code = self.retention_mgr.cleanup_uploaded_files_if_needed()
            # missing file should be marked as deleted in DB
            rows = self.db.get_uploaded_recordings()
            self.assertEqual(len(rows), 0)

    def test_idempotent_cleanup(self):
        """Case 8: Running cleanup repeatedly is safe and idempotent."""
        with patch.object(self.retention_mgr, "get_free_space_bytes", return_value=20 * 1024 ** 3):
            self.assertTrue(self.retention_mgr.cleanup_uploaded_files_if_needed()[0])
            self.assertTrue(self.retention_mgr.cleanup_uploaded_files_if_needed()[0])


if __name__ == "__main__":
    unittest.main()
