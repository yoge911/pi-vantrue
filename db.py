import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class SyncDB:
    """SQLite Database Manager for tracking Vantrue recording sync state."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
        except sqlite3.OperationalError:
            pass  # Fallback if filesystem does not support WAL
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS recordings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_url TEXT UNIQUE NOT NULL,
                    filename TEXT NOT NULL,
                    file_size INTEGER DEFAULT 0,
                    recording_timestamp TEXT,
                    status TEXT NOT NULL CHECK(status IN ('discovered', 'downloaded', 'uploaded', 'deleted')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_recordings_status
                ON recordings(status);
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_recordings_timestamp
                ON recordings(recording_timestamp);
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS preservation_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT UNIQUE NOT NULL,
                    from_time TEXT NOT NULL,
                    to_time TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_preservation_status
                ON preservation_requests(status);
                """
            )
            conn.commit()

    def register_recordings(self, recordings: List[Dict]):
        """Register newly discovered recordings into database."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            for rec in recordings:
                cursor.execute(
                    """
                    INSERT INTO recordings (
                        remote_url, filename, file_size, recording_timestamp, status
                    ) VALUES (?, ?, ?, ?, 'discovered')
                    ON CONFLICT(remote_url) DO UPDATE SET
                        file_size = COALESCE(EXCLUDED.file_size, recordings.file_size),
                        recording_timestamp = COALESCE(EXCLUDED.recording_timestamp, recordings.recording_timestamp)
                    """,
                    (
                        rec["remote_url"],
                        rec["filename"],
                        rec.get("file_size", 0),
                        rec.get("recording_timestamp", ""),
                    ),
                )
            conn.commit()

    def get_pending_downloads(self) -> List[sqlite3.Row]:
        """Fetch discovered recordings sorted chronologically oldest-first."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM recordings
                WHERE status = 'discovered'
                ORDER BY recording_timestamp ASC, filename ASC;
                """
            )
            return cursor.fetchall()

    def mark_downloaded(self, remote_url: str, file_size: int):
        """Mark a recording as successfully downloaded."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE recordings
                SET status = 'downloaded',
                    file_size = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE remote_url = ?;
                """,
                (file_size, remote_url),
            )
            conn.commit()

    def is_already_downloaded_or_synced(self, remote_url: str) -> bool:
        """Check if remote_url has already been downloaded or uploaded."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT status FROM recordings WHERE remote_url = ?;
                """,
                (remote_url,),
            )
            row = cursor.fetchone()
            if row and row["status"] in ("downloaded", "uploaded"):
                return True
            return False

    def get_pending_uploads(self) -> List[sqlite3.Row]:
        """Fetch downloaded recordings awaiting upload sorted chronologically oldest-first."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM recordings
                WHERE status = 'downloaded'
                ORDER BY recording_timestamp ASC, filename ASC;
                """
            )
            return cursor.fetchall()

    def mark_uploaded(self, remote_url: str):
        """Mark a recording as successfully uploaded to cloud storage."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE recordings
                SET status = 'uploaded',
                    updated_at = CURRENT_TIMESTAMP
                WHERE remote_url = ?;
                """,
                (remote_url,),
            )
            conn.commit()

    def mark_deleted(self, remote_url: str):
        """Mark a recording as deleted locally after confirmed cloud upload."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE recordings
                SET status = 'deleted',
                    updated_at = CURRENT_TIMESTAMP
                WHERE remote_url = ?;
                """,
                (remote_url,),
            )
            conn.commit()

    def get_downloaded_buffer_size(self) -> int:
        """Calculate total bytes of files currently marked as downloaded."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT SUM(file_size) as total_size FROM recordings WHERE status = 'downloaded';
                """
            )
            row = cursor.fetchone()
            if row and row["total_size"]:
                return int(row["total_size"])
            return 0

    def add_preservation_request(
        self, request_id: str, from_time: str, to_time: str, status: str = "pending"
    ) -> Tuple[bool, str]:
        """
        Insert a new preservation request idempotently.
        Returns (True, "created") on success, or (False, "already_exists") on duplicate request_id.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO preservation_requests (request_id, from_time, to_time, status)
                    VALUES (?, ?, ?, ?);
                    """,
                    (request_id, from_time, to_time, status),
                )
                conn.commit()
                return True, "created"
            except sqlite3.IntegrityError:
                return False, "already_exists"

    def get_pending_preservation_requests(self) -> List[sqlite3.Row]:
        """Fetch all pending preservation requests ordered by creation time."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM preservation_requests
                WHERE status = 'pending'
                ORDER BY created_at ASC;
                """
            )
            return cursor.fetchall()
