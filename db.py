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
        conn = sqlite3.connect(str(self.db_path), timeout=20.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
        except Exception:
            pass
        try:
            conn.execute("PRAGMA busy_timeout=5000;")
        except Exception:
            pass
        return conn

    def _init_db(self):
        try:
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
                # Automatic idempotent schema migration for uploaded_at timestamp
                try:
                    cursor.execute("ALTER TABLE recordings ADD COLUMN uploaded_at TIMESTAMP;")
                except Exception:
                    pass

                conn.commit()
        except sqlite3.OperationalError:
            pass

    def sync_physical_files(self, download_dir: Path):
        """
        Scan download_dir for physical video files and reconcile DB status.
        If a file exists on disk but is marked 'deleted' or missing from DB,
        update status to 'downloaded' (or 'uploaded' if previously uploaded).
        """
        if not download_dir.exists():
            return
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                for entry in download_dir.iterdir():
                    if entry.is_file() and not entry.name.endswith(".part"):
                        fn = entry.name
                        size = entry.stat().st_size
                        cursor.execute("SELECT * FROM recordings WHERE filename = ?;", (fn,))
                        row = cursor.fetchone()
                        if row:
                            if row["status"] == "deleted":
                                new_status = "uploaded" if (row["uploaded_at"] if "uploaded_at" in row.keys() else None) else "downloaded"
                                cursor.execute(
                                    "UPDATE recordings SET status = ?, file_size = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                                    (new_status, size, row["id"]),
                                )
                        else:
                            dummy_url = f"local://{fn}"
                            cursor.execute(
                                """
                                INSERT INTO recordings (remote_url, filename, file_size, recording_timestamp, status)
                                VALUES (?, ?, ?, ?, 'downloaded')
                                ON CONFLICT(remote_url) DO UPDATE SET status = 'downloaded', file_size = EXCLUDED.file_size;
                                """,
                                (dummy_url, fn, size, fn),
                            )
                conn.commit()
        except Exception:
            pass


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

    def get_uploaded_recordings(self) -> List[sqlite3.Row]:
        """Fetch all uploaded recordings sorted chronologically oldest-first for rolling cache cleanup."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM recordings
                WHERE status = 'uploaded'
                ORDER BY recording_timestamp ASC, filename ASC;
                """
            )
            return cursor.fetchall()

    def get_all_recordings(self, filter_status: Optional[str] = None, sort_desc: bool = True) -> List[sqlite3.Row]:
        """Fetch recordings with optional status filter and timestamp ordering."""
        order = "DESC" if sort_desc else "ASC"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if filter_status and filter_status != "all":
                cursor.execute(
                    f"""
                    SELECT * FROM recordings
                    WHERE status = ?
                    ORDER BY recording_timestamp {order}, filename {order};
                    """,
                    (filter_status,),
                )
            else:
                cursor.execute(
                    f"""
                    SELECT * FROM recordings
                    ORDER BY recording_timestamp {order}, filename {order};
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
                    uploaded_at = CURRENT_TIMESTAMP,
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

    def get_stats(self) -> Dict:
        """Get summary statistics for dashboard display."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    COUNT(*) as total_discovered,
                    SUM(CASE WHEN status = 'downloaded' THEN 1 ELSE 0 END) as pending_upload_count,
                    SUM(CASE WHEN status = 'uploaded' THEN 1 ELSE 0 END) as uploaded_count,
                    SUM(CASE WHEN status in ('downloaded', 'uploaded') THEN 1 ELSE 0 END) as local_count,
                    SUM(CASE WHEN status in ('downloaded', 'uploaded') THEN file_size ELSE 0 END) as local_size,
                    MAX(CASE WHEN status = 'uploaded' THEN updated_at ELSE NULL END) as last_uploaded_at
                FROM recordings;
                """
            )
            row = cursor.fetchone()
            return {
                "total_discovered": row["total_discovered"] or 0,
                "pending_upload_count": row["pending_upload_count"] or 0,
                "uploaded_count": row["uploaded_count"] or 0,
                "local_count": row["local_count"] or 0,
                "local_size": row["local_size"] or 0,
                "last_uploaded_at": row["last_uploaded_at"],
            }

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

