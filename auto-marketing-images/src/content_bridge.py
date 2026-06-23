from __future__ import annotations

import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from src.content_queue import ContentQueue

LOGGER = logging.getLogger(__name__)


def _parse_multipart(body: bytes, content_type: str) -> dict[str, list[tuple[str | None, bytes]]]:
    match = re.search(r"boundary=(.+)", content_type, re.I)
    if not match:
        raise ValueError("multipart boundary missing")
    boundary = match.group(1).strip().strip('"')
    delimiter = f"--{boundary}".encode()
    parts: dict[str, list[tuple[str | None, bytes]]] = {}

    for chunk in body.split(delimiter):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        header_end = chunk.find(b"\r\n\r\n")
        if header_end < 0:
            continue
        header_block = chunk[:header_end].decode("utf-8", errors="replace")
        payload = chunk[header_end + 4 :]
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        name_match = re.search(r'name="([^"]+)"', header_block)
        if not name_match:
            continue
        name = name_match.group(1)
        filename = None
        fn_match = re.search(r'filename="([^"]*)"', header_block)
        if fn_match:
            filename = fn_match.group(1)
        parts.setdefault(name, []).append((filename, payload))
    return parts


class ContentBridgeServer:
    """Lightweight HTTP server for plugin → disk content handoff."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        queue: ContentQueue,
        auth_token: str = "",
        max_payload_bytes: int = 52_428_800,
    ) -> None:
        self.host = host
        self.port = port
        self.queue = queue
        self.auth_token = (auth_token or "").strip()
        self.max_payload_bytes = max_payload_bytes
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        handler_cls = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler_cls)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="content-bridge", daemon=True
        )
        self._thread.start()
        LOGGER.info("Content bridge listening on http://%s:%s", self.host, self.port)

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        LOGGER.info("Content bridge stopped.")


def _make_handler(bridge: ContentBridgeServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ContentBridge/1.0"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            LOGGER.debug("bridge: " + format, *args)

        def _check_auth(self) -> bool:
            if not bridge.auth_token:
                client = self.client_address[0]
                if client not in ("127.0.0.1", "::1"):
                    self._json_response(403, {"error": "auth_token required for non-localhost"})
                    return False
                return True
            header = self.headers.get("Authorization", "")
            if header == f"Bearer {bridge.auth_token}":
                return True
            token = self.headers.get("X-Bridge-Token", "")
            if token == bridge.auth_token:
                return True
            self._json_response(401, {"error": "unauthorized"})
            return False

        def _json_response(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/health":
                self._json_response(200, {"status": "ok"})
                return
            if path == "/content/stats":
                self._json_response(200, {"pending": bridge.queue.pending_count()})
                return
            self._json_response(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._check_auth():
                return
            path = urlparse(self.path).path
            if path != "/content":
                self._json_response(404, {"error": "not found"})
                return

            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                self._json_response(400, {"error": "expected multipart/form-data"})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > bridge.max_payload_bytes:
                    self._json_response(413, {"error": "payload too large"})
                    return
                raw = self.rfile.read(length)
                form = _parse_multipart(raw, content_type)
                manifest_parts = form.get("manifest")
                if not manifest_parts:
                    self._json_response(400, {"error": "manifest field required"})
                    return
                manifest_raw = manifest_parts[0][1].decode("utf-8")
                manifest_data = json.loads(manifest_raw)

                images: list[tuple[str, bytes]] = []
                for key in sorted(form.keys()):
                    if not str(key).startswith("image_"):
                        continue
                    for _filename, blob in form[key]:
                        images.append((key, blob))

                if not images:
                    self._json_response(400, {"error": "at least one image_* field required"})
                    return

                package_dir = bridge.queue.ingest_multipart(
                    manifest_data,
                    images,
                    max_payload_bytes=bridge.max_payload_bytes,
                )
                self._json_response(
                    201,
                    {"id": package_dir.name, "path": str(package_dir)},
                )
            except FileExistsError as exc:
                self._json_response(409, {"error": str(exc)})
            except (ValueError, json.JSONDecodeError) as exc:
                self._json_response(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("Bridge POST failed")
                self._json_response(500, {"error": str(exc)})

    return Handler


def build_bridge_from_config(root: Path, pipeline: dict[str, Any]) -> ContentBridgeServer | None:
    cfg = pipeline.get("content_bridge") or {}
    if not cfg.get("enabled", True):
        return None
    inbox = root / str(cfg.get("inbox_dir") or "data/pending_content")
    processed = root / str(cfg.get("processed_dir") or "data/processed_content")
    queue = ContentQueue(inbox_dir=inbox, processed_dir=processed)
    return ContentBridgeServer(
        host=str(cfg.get("host") or "127.0.0.1"),
        port=int(cfg.get("port") or 8765),
        queue=queue,
        auth_token=str(cfg.get("auth_token") or ""),
        max_payload_bytes=int(cfg.get("max_payload_bytes") or 52_428_800),
    )
