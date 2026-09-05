import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from web_server import CloudStreamCache


class TestCloudStreamCache(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.temp_dir.name)
        self.cache = CloudStreamCache(
            cache_dir=self.cache_dir,
            prefetch_bytes=1024,  # Use small prefetch size for test (1KB)
            max_files=3,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("subprocess.run")
    def test_prefetch_and_get_cached_chunk(self, mock_subprocess_run):
        # Mock rclone cat returning 1024 bytes of data
        fake_data = b"X" * 1024
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = fake_data
        mock_subprocess_run.return_value = mock_res

        # Request bytes 0-1 (2 bytes) out of 2048 total
        chunk = self.cache.get_cached_chunk(
            filename="TEST_0001.MP4",
            remote_target="gdrive:vantrue/TEST_0001.MP4",
            start_byte=0,
            end_byte=1,
            total_file_size=2048,
        )

        self.assertIsNotNone(chunk)
        self.assertEqual(len(chunk), 2)
        self.assertEqual(chunk, b"XX")

        # Subprocess should have been called once for prefetch
        mock_subprocess_run.assert_called_once()
        cmd = mock_subprocess_run.call_args[0][0]
        self.assertIn("rclone", cmd)
        self.assertIn("cat", cmd)
        self.assertIn("1024", cmd)

        # Second request for bytes 10-20 should be served directly from disk without calling rclone again
        mock_subprocess_run.reset_mock()
        chunk2 = self.cache.get_cached_chunk(
            filename="TEST_0001.MP4",
            remote_target="gdrive:vantrue/TEST_0001.MP4",
            start_byte=10,
            end_byte=20,
            total_file_size=2048,
        )
        self.assertIsNotNone(chunk2)
        self.assertEqual(len(chunk2), 11)
        mock_subprocess_run.assert_not_called()

    def test_out_of_cache_range_returns_none(self):
        # Create a pre-cached file of 1024 bytes directly
        safe_fn = "TEST_0002_MP4"
        cache_file = self.cache_dir / f"{safe_fn}.part0"
        cache_file.write_bytes(b"Y" * 1024)

        # Request start_byte >= prefetch_bytes (1024)
        chunk = self.cache.get_cached_chunk(
            filename="TEST_0002.MP4",
            remote_target="gdrive:vantrue/TEST_0002.MP4",
            start_byte=1024,
            end_byte=2047,
            total_file_size=4096,
        )
        self.assertIsNone(chunk)

    @patch("subprocess.run")
    def test_cache_eviction(self, mock_subprocess_run):
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = b"Z" * 1024
        mock_subprocess_run.return_value = mock_res

        # Fill cache up to max_files (3)
        for i in range(3):
            fn = f"FILE_{i}.MP4"
            self.cache.get_cached_chunk(fn, f"gdrive:{fn}", 0, 1, 2048)

        cached_files = list(self.cache_dir.glob("*.part0"))
        self.assertEqual(len(cached_files), 3)

        # Add 4th file to trigger eviction of oldest
        self.cache.get_cached_chunk("FILE_3.MP4", "gdrive:FILE_3.MP4", 0, 1, 2048)
        cached_files_after = list(self.cache_dir.glob("*.part0"))
        self.assertLessEqual(len(cached_files_after), 3)


if __name__ == "__main__":
    unittest.main()
