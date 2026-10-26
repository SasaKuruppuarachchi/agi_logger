"""Browser log reader for agi_logger flight sessions (stdlib HTTP server + single-page UI, no internet needed).

  python3 -m agi_logger.reader.server [--root <session_path>] [--port 8080] [--host 127.0.0.1]
  agi-logger reader ...                 (same, from the CLI)

API (JSON unless noted):
  GET /api/sessions                          session list (newest first)
  GET /api/sessions/<id>/summary[?rebuild=1] time series, timelines, events, thumbnail index (cached)
  GET /api/sessions/<id>/manifest            the session manifest
  GET /api/sessions/<id>/thumbs/<file>       camera thumbnail (JPEG)
  GET /api/sessions/<id>/files/<relpath>     any file inside the session (configs, stats, logs, MCAPs)

Bind to 127.0.0.1 by default; use --host 0.0.0.0 to browse from a laptop on the drone's network.
Paths outside the session folder are refused.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from .summary import get_summary, list_sessions

STATIC = Path(__file__).with_name("static")
_build_lock = threading.Lock()  # one summary build at a time (rosbag2 reader + rclpy deserialisation)


def make_handler(root: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "agi-logger-reader/1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str, cache: bool = False) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: Any, code: int = 200) -> None:
            self._send(code, json.dumps(data, separators=(",", ":")).encode(), "application/json")

        def _file(self, path: Path, cache: bool = False) -> None:
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if path.suffix in (".yaml", ".yml", ".log", ".txt", ".csv") or path.name.startswith("launch_params_"):
                ctype = "text/plain; charset=utf-8"
            self._send(200, path.read_bytes(), ctype, cache)

        def _session_dir(self, sid: str) -> Optional[Path]:
            d = (root / sid).resolve()
            return d if d.parent == root.resolve() and (d / "manifest.json").is_file() else None

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            url = urllib.parse.urlparse(self.path)
            parts = [urllib.parse.unquote(p) for p in url.path.strip("/").split("/") if p]
            query = urllib.parse.parse_qs(url.query)
            try:
                if not parts or parts == ["index.html"]:
                    return self._file(STATIC / "index.html")
                if parts[0] != "api":
                    return self._send(404, b"not found", "text/plain")
                if parts[1:] == ["sessions"]:
                    return self._json(list_sessions(root))
                if len(parts) >= 4 and parts[1] == "sessions":
                    sdir = self._session_dir(parts[2])
                    if sdir is None:
                        return self._json({"error": "unknown session"}, 404)
                    what = parts[3]
                    if what == "summary":
                        with _build_lock:
                            return self._json(get_summary(sdir, rebuild="rebuild" in query))
                    if what == "manifest":
                        return self._file(sdir / "manifest.json")
                    if what in ("thumbs", "files") and len(parts) >= 5:
                        base = sdir / ".reader" / "thumbs" if what == "thumbs" else sdir
                        target = (base / "/".join(parts[4:])).resolve()
                        if sdir.resolve() not in target.parents or not target.is_file():
                            return self._send(404, b"not found", "text/plain")
                        return self._file(target, cache=(what == "thumbs"))
                return self._send(404, b"not found", "text/plain")
            except BrokenPipeError:
                pass
            except Exception as exc:  # report, never kill the server
                self._json({"error": f"{type(exc).__name__}: {exc}"}, int(HTTPStatus.INTERNAL_SERVER_ERROR))

    return Handler


def serve(root: Path, host: str = "127.0.0.1", port: int = 8080) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(root))
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"agi-logger reader: http://{shown}:{port}/  (sessions in {root}; Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def default_root(config_path: Optional[Path] = None) -> Path:
    from ..config import load_raw_config
    try:
        cfg = load_raw_config(config_path)["agi_logger"].get("flight_session", {}) or {}
        return Path(str(cfg.get("session_path", "~/agi_flights"))).expanduser()
    except Exception:
        return Path("~/agi_flights").expanduser()


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="agi_logger flight-session log reader (browser)")
    ap.add_argument("--root", help="folder with flight sessions (default: flight_session.session_path)")
    ap.add_argument("--config", help="agi_logger configs.yaml")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args(argv)
    root = Path(a.root).expanduser() if a.root else default_root(Path(a.config) if a.config else None)
    serve(root, a.host, a.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
