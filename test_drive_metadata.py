import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from config import Config
from db import SyncDB
from uploader import VantrueUploader


class TestDriveMetadataAndPlayback(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.video_dir = self.temp_dir / "videos"
        self.video_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.temp_dir / "sync_state.db"

        class TestConfig(Config):
            LOCAL_DOWNLOAD_DIR = self.video_dir
            DB_PATH = self.db_path
            RCLONE_REMOTE = "gdrive:"
            RCLONE_DESTINATION = "Vantrue"
            IS_EXPLICIT_BASE_URL = True

        self.config = TestConfig
        self.db = SyncDB(self.db_path)
        self.uploader = VantrueUploader(self.config)
        self.uploader.check_internet_connectivity = MagicMock(return_value=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_1_new_cloud_upload_verification_and_drive_id(self):
        """TEST 1: New cloud upload runs copyto + lsjson --stat size matching -> stores drive_file_id."""
        local_file = self.video_dir / "test_vid_01.mp4"
        local_file.write_bytes(b"A" * 1024)  # 1024 bytes

        remote_url = "http://cam/Normal/test_vid_01.mp4"
        rec = {
            "remote_url": remote_url,
            "filename": "test_vid_01.mp4",
            "file_size": 1024,
            "recording_timestamp": "2026-09-04 10:00:00",
            "priority": 1,
            "status": "downloaded",
        }
        self.db.register_recordings([rec])
        self.db.mark_downloaded(remote_url, 1024)

        # Mock rclone copyto success and rclone lsjson --stat returning exact object metadata
        mock_lsjson_stdout = json.dumps({
            "Path": "test_vid_01.mp4",
            "Name": "test_vid_01.mp4",
            "Size": 1024,
            "ID": "DRIVE_FILE_ID_12345"
        })

        def mock_subprocess_run(cmd, capture_output=True, text=True, timeout=None):
            m = MagicMock()
            m.returncode = 0
            if "lsjson" in cmd:
                self.assertIn("--stat", cmd)
                m.stdout = mock_lsjson_stdout
            else:
                m.stdout = ""
            return m

        with patch("subprocess.run", side_effect=mock_subprocess_run):
            self.uploader.run_upload_cycle()

        # Verify SQLite record updated with status='uploaded' and drive_file_id
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT status, drive_file_id FROM recordings WHERE remote_url = ?;", (remote_url,)).fetchone()
            self.assertEqual(row["status"], "uploaded")
            self.assertEqual(row["drive_file_id"], "DRIVE_FILE_ID_12345")

    def test_2_waiting_upload_has_no_drive_url(self):
        """TEST 2: Waiting upload has status='downloaded' and drive_file_id=None."""
        remote_url = "http://cam/Normal/test_vid_waiting.mp4"
        rec = {
            "remote_url": remote_url,
            "filename": "test_vid_waiting.mp4",
            "file_size": 2048,
            "recording_timestamp": "2026-09-04 11:00:00",
            "priority": 1,
            "status": "discovered",
        }
        self.db.register_recordings([rec])
        self.db.mark_downloaded(remote_url, 2048)

        records = self.db.get_all_recordings()
        r = records[0]
        self.assertEqual(r["status"], "downloaded")
        self.assertIsNone(dict(r).get("drive_file_id"))

    def test_5_historical_cloud_synced_backfill(self):
        """TEST 5: Throttled backfill resolves drive_file_id for existing uploaded records without re-uploading."""
        remote_url1 = "http://cam/Normal/hist_01.mp4"
        remote_url2 = "http://cam/Normal/hist_02.mp4"

        self.db.register_recordings([
            {"remote_url": remote_url1, "filename": "hist_01.mp4", "recording_timestamp": "2026-09-04 09:00:00", "priority": 1},
            {"remote_url": remote_url2, "filename": "hist_02.mp4", "recording_timestamp": "2026-09-04 08:00:00", "priority": 1},
        ])
        self.db.mark_uploaded(remote_url1)
        self.db.mark_uploaded(remote_url2)

        # Confirm initial state: drive_file_id is None
        missing = self.db.get_recordings_missing_drive_id(limit=10)
        self.assertEqual(len(missing), 2)

        mock_lsjson = json.dumps({"Path": "hist_01.mp4", "Size": 500, "ID": "HIST_DRIVE_ID_01"})

        def mock_subprocess_run(cmd, capture_output=True, text=True, timeout=None):
            m = MagicMock()
            m.returncode = 0
            if "lsjson" in cmd:
                self.assertIn("--stat", cmd)
                m.stdout = mock_lsjson
            return m

        with patch("subprocess.run", side_effect=mock_subprocess_run):
            self.uploader.backfill_missing_drive_ids(max_batch=1)

        # Batch limit 1 -> exactly 1 record updated, 1 remaining
        missing_after = self.db.get_recordings_missing_drive_id(limit=10)
        self.assertEqual(len(missing_after), 1)

    def test_6_metadata_lookup_failure_graceful_handling(self):
        """TEST 6: Metadata lookup failure after copyto does not crash or mark file unsynced."""
        local_file = self.video_dir / "test_vid_fail_meta.mp4"
        local_file.write_bytes(b"B" * 512)

        remote_url = "http://cam/Normal/test_vid_fail_meta.mp4"
        self.db.register_recordings([
            {"remote_url": remote_url, "filename": "test_vid_fail_meta.mp4", "file_size": 512, "recording_timestamp": "2026-09-04 12:00:00", "priority": 1}
        ])
        self.db.mark_downloaded(remote_url, 512)

        def mock_subprocess_run(cmd, capture_output=True, text=True, timeout=None):
            m = MagicMock()
            if "copyto" in cmd:
                m.returncode = 0
                m.stdout = ""
            else:
                # lsjson --stat fails with error
                m.returncode = 1
                m.stdout = ""
                m.stderr = "API rate limit exceeded"
            return m

        with patch("subprocess.run", side_effect=mock_subprocess_run):
            self.uploader.run_upload_cycle()

        # File is still marked uploaded (cloud synced), drive_file_id is pending backfill
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT status, drive_file_id FROM recordings WHERE remote_url = ?;", (remote_url,)).fetchone()
            self.assertEqual(row["status"], "uploaded")
            self.assertIsNone(row["drive_file_id"])

    def test_7_service_restart_persists_drive_id(self):
        """TEST 7: Database state persists drive_file_id across service restarts."""
        remote_url = "http://cam/Normal/restart_test.mp4"
        self.db.register_recordings([
            {"remote_url": remote_url, "filename": "restart_test.mp4", "recording_timestamp": "2026-09-04 13:00:00", "priority": 1}
        ])
        self.db.mark_uploaded(remote_url, drive_file_id="RESTART_DRIVE_ID_999")

        # Simulate new process instance with fresh SyncDB instance on same database file
        new_db = SyncDB(self.db_path)
        records = new_db.get_all_recordings()
        r = records[0]
        self.assertEqual(r["status"], "uploaded")
        self.assertEqual(r["drive_file_id"], "RESTART_DRIVE_ID_999")


if __name__ == "__main__":
    unittest.main()
