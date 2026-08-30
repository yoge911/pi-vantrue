import json
import sys
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict

from config import Config
from db import SyncDB


class PreserveAPIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for iPhone Preservation Requests."""

    def get_db(self) -> SyncDB:
        """Get SyncDB connection instance for active DB_PATH."""
        return SyncDB(Config.DB_PATH)

    def log_message(self, format_str: str, *args: Any):
        """Override log_message to output journal-friendly log messages."""
        sys.stdout.write(f"[PreserveAPI] {args[0]} - {args[1]}\n")
        sys.stdout.flush()

    def _send_json(self, status_code: int, data: Dict[str, Any]):
        """Helper to send JSON response."""
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        """Handle GET requests."""
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
        else:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"status": "error", "message": "Endpoint not found"},
            )

    def do_POST(self):
        """Handle POST /preserve requests."""
        if self.path != "/preserve":
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"status": "error", "message": "Endpoint not found"},
            )
            return

        content_length_header = self.headers.get("Content-Length")
        if not content_length_header or not content_length_header.isdigit():
            print("[PreserveAPI] Invalid request: Missing Content-Length header.", flush=True)
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"status": "error", "message": "Missing or invalid Content-Length header"},
            )
            return

        content_length = int(content_length_header)
        if content_length > Config.MAX_HTTP_BODY_BYTES:
            print(f"[PreserveAPI] Invalid request: Payload size ({content_length} bytes) exceeds limit.", flush=True)
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"status": "error", "message": "Payload size exceeds maximum allowed limit"},
            )
            return

        try:
            raw_body = self.rfile.read(content_length)
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(f"[PreserveAPI] Invalid request: Failed to parse JSON body ({exc}).", flush=True)
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"status": "error", "message": "Invalid JSON request body"},
            )
            return

        if not isinstance(payload, dict):
            print("[PreserveAPI] Invalid request: Request body must be a JSON object.", flush=True)
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"status": "error", "message": "Request body must be a JSON object"},
            )
            return

        # Validate required fields
        request_id = payload.get("request_id")
        from_str = payload.get("from")
        to_str = payload.get("to")
        status_val = payload.get("status")

        if not request_id or not isinstance(request_id, str) or not request_id.strip():
            print("[PreserveAPI] Invalid request: 'request_id' is missing or empty.", flush=True)
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"status": "error", "message": "'request_id' is required and must be a non-empty string"},
            )
            return

        if status_val != "pending":
            print(f"[PreserveAPI] Invalid request: 'status' must be 'pending' (got '{status_val}').", flush=True)
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"status": "error", "message": "'status' must be exactly 'pending'"},
            )
            return

        if not from_str or not isinstance(from_str, str):
            print("[PreserveAPI] Invalid request: 'from' timestamp is missing.", flush=True)
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"status": "error", "message": "'from' timestamp is required and must be a string"},
            )
            return

        if not to_str or not isinstance(to_str, str):
            print("[PreserveAPI] Invalid request: 'to' timestamp is missing.", flush=True)
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"status": "error", "message": "'to' timestamp is required and must be a string"},
            )
            return

        # Validate ISO-8601 datetimes
        try:
            from_dt = datetime.fromisoformat(from_str.replace("Z", "+00:00"))
        except Exception:
            print(f"[PreserveAPI] Invalid request: Cannot parse 'from' ISO-8601 timestamp '{from_str}'.", flush=True)
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"status": "error", "message": "'from' must be a valid ISO-8601 datetime string"},
            )
            return

        try:
            to_dt = datetime.fromisoformat(to_str.replace("Z", "+00:00"))
        except Exception:
            print(f"[PreserveAPI] Invalid request: Cannot parse 'to' ISO-8601 timestamp '{to_str}'.", flush=True)
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"status": "error", "message": "'to' must be a valid ISO-8601 datetime string"},
            )
            return

        if from_dt >= to_dt:
            print(f"[PreserveAPI] Invalid request: 'from' ({from_str}) is not earlier than 'to' ({to_str}).", flush=True)
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"status": "error", "message": "'from' timestamp must be strictly earlier than 'to' timestamp"},
            )
            return

        # Persist request into SQLite
        try:
            created, result_code = self.get_db().add_preservation_request(
                request_id=request_id.strip(),
                from_time=from_str.strip(),
                to_time=to_str.strip(),
                status="pending",
            )
        except Exception as exc:
            print(f"[PreserveAPI] Database error while storing request '{request_id}': {exc}", flush=True)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"status": "error", "message": "internal server error"},
            )
            return

        if created:
            print(f"[PreserveAPI] Accepted request {request_id}", flush=True)
            self._send_json(
                HTTPStatus.ACCEPTED,
                {"status": "accepted", "request_id": request_id},
            )
        else:
            print(f"[PreserveAPI] Request already exists {request_id}", flush=True)
            self._send_json(
                HTTPStatus.OK,
                {"status": "already_exists", "request_id": request_id},
            )


def main():
    host = Config.PRESERVE_API_HOST
    port = Config.PRESERVE_API_PORT
    server = HTTPServer((host, port), PreserveAPIHandler)
    print(f"[PreserveAPI] Listening on {host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[PreserveAPI] Server shutting down.", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
