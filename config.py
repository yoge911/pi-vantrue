import os
from pathlib import Path


class Config:
    """Central configuration for Vantrue Automation System."""

    # Flag indicating whether VANTRUE_BASE_URL was explicitly provided in environment
    IS_EXPLICIT_BASE_URL = "VANTRUE_BASE_URL" in os.environ

    # Base URL for Vantrue dashcam HTTP interface or Mac mock server
    VANTRUE_BASE_URL = os.environ.get("VANTRUE_BASE_URL", "http://127.0.0.1:8000/").rstrip("/") + "/"

    # Local video buffer directory
    LOCAL_DOWNLOAD_DIR = Path(
        os.environ.get("LOCAL_DOWNLOAD_DIR", "/home/picar/vantrue-videos")
    )

    # SQLite Database Path
    DB_PATH = Path(
        os.environ.get(
            "DB_PATH",
            "/home/picar/vantrue-automation/pi-vantrue/sync_state.db",
        )
    )

    # Maximum local video buffer size limit (Default: 10 GB)
    MAX_BUFFER_BYTES = int(
        os.environ.get("MAX_BUFFER_BYTES", 10 * 1024 * 1024 * 1024)
    )

    # Minimum free disk space safety reserve (Default: 5 GB)
    MIN_FREE_DISK_BYTES = int(
        os.environ.get("MIN_FREE_DISK_BYTES", 5 * 1024 * 1024 * 1024)
    )

    # Supported video file extensions
    SUPPORTED_EXTENSIONS = (
        ".mp4",
        ".mov",
        ".ts",
        ".avi",
    )

    # HTTP connection & read timeout in seconds
    HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", 15))

    # Chunk size for streaming HTTP downloads (128 KB)
    DOWNLOAD_CHUNK_SIZE = 128 * 1024
