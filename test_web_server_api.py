import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch, MagicMock

from db import SyncDB
import web_server


class DummyWFile:
    def __init__(self):
        self.buffer = BytesIO()

    def write(self, data):
        self.buffer.write(data)

    def getvalue(self):
        return self.buffer.getvalue()


class TestWebServerAPI(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_sync.db"
        self.db = SyncDB(self.db_path)

        # Seed test recordings with various statuses and file sizes
        self.db.register_recordings([
            {
                "remote_url": "http://192.168.1.254/20260905_120000_0001_A.MP4",
                "filename": "20260905_120000_0001_A.MP4",
                "file_size": 104857600,  # 100 MB
                "recording_timestamp": "2026-09-05T12:00:00Z"
            },
            {
                "remote_url": "http://192.168.1.254/20260905_120000_0002_B.MP4",
                "filename": "20260905_120000_0002_B.MP4",
                "file_size": None,  # Test None file size handling
                "recording_timestamp": "2026-09-05T12:01:00Z"
            }
        ])

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("web_server.Config")
    @patch("web_server.SyncDB")
    def test_handle_api_status_json_structure(self, mock_syncdb, mock_config):
        mock_config.DB_PATH = self.db_path
        mock_config.LOCAL_DOWNLOAD_DIR = Path(self.temp_dir.name)
        mock_config.MIN_FREE_SPACE_GB = 15
        mock_config.VANTRUE_INTERFACE = "wlan0"
        mock_config.INTERNET_INTERFACE = "wlan1"
        mock_syncdb.return_value = self.db

        handler = MagicMock()
        handler.db = self.db

        wfile = DummyWFile()
        handler.wfile = wfile

        captured_json = {}

        def fake_send_json(status_code, data):
            nonlocal captured_json
            captured_json = data

        handler._send_json = fake_send_json

        web_server.VantrueWebHandler._handle_api_status(handler)

        self.assertIn("storage", captured_json)
        self.assertIn("queue", captured_json)
        self.assertIn("videos", captured_json)
        self.assertIn("network", captured_json)
        self.assertIn("wlan0", captured_json["network"])
        self.assertIn("wlan1", captured_json["network"])

    @patch("web_server.Config")
    @patch("web_server.SyncDB")
    def test_handle_api_videos_none_file_size(self, mock_syncdb, mock_config):
        mock_config.DB_PATH = self.db_path
        mock_config.LOCAL_DOWNLOAD_DIR = Path(self.temp_dir.name)
        mock_syncdb.return_value = self.db

        handler = MagicMock()
        captured_json = {}

        def fake_send_json(status_code, data):
            nonlocal captured_json
            captured_json = data

        handler._send_json = fake_send_json

        web_server.VantrueWebHandler._handle_api_videos(handler, "status=all")

        self.assertIn("videos", captured_json)
        videos = captured_json["videos"]
        self.assertGreaterEqual(len(videos), 2)
        # Check that file_size = None was safely handled without crash
        for v in videos:
            self.assertIsNotNone(v["file_size_mb"])


if __name__ == "__main__":
    unittest.main()
