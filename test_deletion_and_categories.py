import os
import tempfile
import unittest
from pathlib import Path

from db import SyncDB
from web_server import categorize_filename


class TestDeletionAndCategories(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_sync.db"
        self.db = SyncDB(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_db_update_recording_status(self):
        # Insert test recording
        self.db.register_recordings([{
            "remote_url": "http://192.168.1.254/20260905_120000_0001_A.MP4",
            "filename": "20260905_120000_0001_A.MP4",
            "file_size": 1024,
            "recording_timestamp": "2026-09-05T12:00:00Z"
        }])
        self.db.update_recording_status("20260905_120000_0001_A.MP4", "uploaded", drive_file_id="drive123")

        rec = self.db.get_recording_by_filename("20260905_120000_0001_A.MP4")
        self.assertEqual(rec["status"], "uploaded")
        self.assertEqual(rec["drive_file_id"], "drive123")

        # Update status and clear drive_file_id
        self.db.update_recording_status("20260905_120000_0001_A.MP4", "downloaded", clear_drive_id=True)
        rec = self.db.get_recording_by_filename("20260905_120000_0001_A.MP4")
        self.assertEqual(rec["status"], "downloaded")
        self.assertIsNone(rec["drive_file_id"])

    def test_categorize_filename(self):
        self.assertEqual(categorize_filename("20260905_120000_0001_A.MP4")["position"], "Front")
        self.assertEqual(categorize_filename("20260905_120000_0002_B.MP4")["position"], "Rear")
        self.assertEqual(categorize_filename("20260905_120000_0003_C.MP4")["position"], "Interior")
        self.assertEqual(categorize_filename("20260905_120000_0004_D.MP4")["position"], "Front")


if __name__ == "__main__":
    unittest.main()
