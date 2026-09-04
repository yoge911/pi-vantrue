import os
from pathlib import Path


class Config:
    """Central configuration for Vantrue Automation System."""

    # Dual-Interface Architecture Configuration
    VANTRUE_INTERFACE = os.environ.get("VANTRUE_INTERFACE", "wlan0")
    INTERNET_INTERFACE = os.environ.get("INTERNET_INTERFACE", "wlan1")

    VANTRUE_NETWORK = os.environ.get("VANTRUE_NETWORK", "E3_VANTRUE_13c6")
    IPHONE_NETWORK = os.environ.get("IPHONE_NETWORK", "iPhone 1")

    # wlan1 Hotspot Selection Settings
    PREFERRED_HOTSPOT_SSID = os.environ.get("PREFERRED_HOTSPOT_SSID", "Vantrue-iPhone-Hotspot")
    FALLBACK_HOTSPOT_SSID = os.environ.get("FALLBACK_HOTSPOT_SSID", IPHONE_NETWORK)
    HOTSPOT_PRIORITY = [PREFERRED_HOTSPOT_SSID, FALLBACK_HOTSPOT_SSID]
    HOTSPOT_RETRY_INTERVAL = int(os.environ.get("HOTSPOT_RETRY_INTERVAL", 15))

    # Logging Configuration
    LOG_DIR = Path(
        os.environ.get(
            "LOG_DIR",
            "/home/picar/vantrue-automation/logs",
        )
    )
    LOG_FILE = LOG_DIR / "vantrue.log"
    LOG_MAX_BYTES = int(os.environ.get("LOG_MAX_BYTES", 5 * 1024 * 1024))  # 5 MB
    LOG_BACKUP_COUNT = int(os.environ.get("LOG_BACKUP_COUNT", 5))

    # Flag indicating whether VANTRUE_BASE_URL was explicitly provided in environment
    IS_EXPLICIT_BASE_URL = "VANTRUE_BASE_URL" in os.environ

    # Base URL for Vantrue dashcam HTTP interface or Mac mock server
    VANTRUE_BASE_URL = os.environ.get("VANTRUE_BASE_URL", "http://192.168.1.254/").rstrip("/") + "/"

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

    # Minimum free disk space threshold for rolling cache cleanup (Default: 15 GB)
    MIN_FREE_SPACE_GB = int(os.environ.get("MIN_FREE_SPACE_GB", 15))
    MIN_FREE_DISK_BYTES = int(
        os.environ.get("MIN_FREE_DISK_BYTES", MIN_FREE_SPACE_GB * 1024 * 1024 * 1024)
    )

    # Web Dashboard & Video Streaming Server Configuration
    WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
    WEB_PORT = int(os.environ.get("WEB_PORT", 8080))

    # Priority Classifications (Lower number = Higher Priority)
    PRIORITY_EVENT = 0
    PRIORITY_NORMAL = 1
    PRIORITY_GPS = 2
    PRIORITY_PHOTO = 3
    PRIORITY_OTHER = 4

    # Delay in seconds between file size observations for stability verification
    STABILITY_CHECK_DELAY = float(os.environ.get("STABILITY_CHECK_DELAY", "2.0"))

    # Discovery interval in seconds (Default: 180s / 3 minutes between full remote scans)
    DASHCAM_DISCOVERY_INTERVAL = float(os.environ.get("DASHCAM_DISCOVERY_INTERVAL", "180.0"))

    # Optional maximum recording age threshold in hours (0 = disabled/unlimited)
    DOWNLOAD_MAX_AGE_HOURS = float(os.environ.get("DOWNLOAD_MAX_AGE_HOURS", "0.0"))

    # Supported file extensions (videos, photos, GPS/log metadata)
    SUPPORTED_EXTENSIONS = (
        ".mp4",
        ".mov",
        ".ts",
        ".avi",
        ".jpg",
        ".jpeg",
        ".png",
        ".gps",
        ".dat",
        ".log",
    )

    # HTTP connection & read timeout in seconds
    HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", 15))

    # Chunk size for streaming HTTP downloads (128 KB)
    DOWNLOAD_CHUNK_SIZE = 128 * 1024

    # rclone configuration settings
    RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "gdrive:")
    RCLONE_DESTINATION = os.environ.get("RCLONE_DESTINATION", "Vantrue")

    # Timeout for single file upload via rclone (Default: 300 seconds / 5 minutes)
    RCLONE_UPLOAD_TIMEOUT = int(os.environ.get("RCLONE_UPLOAD_TIMEOUT", 300))

    # Bounded internet connectivity verification URL
    INTERNET_CHECK_URL = os.environ.get(
        "INTERNET_CHECK_URL", "http://connectivitycheck.gstatic.com/generate_204"
    )

    # Preservation Request HTTP API settings
    PRESERVE_API_HOST = os.environ.get("PRESERVE_API_HOST", "0.0.0.0")
    PRESERVE_API_PORT = int(os.environ.get("PRESERVE_API_PORT", 8765))
    MAX_HTTP_BODY_BYTES = int(os.environ.get("MAX_HTTP_BODY_BYTES", 16384))  # 16 KB payload cap


