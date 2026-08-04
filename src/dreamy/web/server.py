from __future__ import annotations

import ipaddress
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit

from ..read import ReadStore
from .api import dispatch, dumps

# `img-src` allows `data:` for the inlined favicon only. Everything else is
# `'self'`, and `object-src 'none'` plus the absence of `unsafe-inline`
# keeps injected markup from executing — the dashboard renders real file
# paths and model-authored text, so that guarantee has to hold.
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)


def _loopback(host: str) -> bool:
    try:
        infos = __import__("socket").getaddrinfo(host, None, type=__import__("socket").SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"host is not resolvable: {host}") from exc
    addresses = {info[4][0] for info in infos}
    if not addresses or not all(ipaddress.ip_address(addr).is_loopback for addr in addresses):
        raise ValueError("web server host must resolve exclusively to loopback addresses")
    return True


def _static_root():
    return resources.files("dreamy.web").joinpath("static")


def build_server(output_dir: Path | str, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    _loopback(host)
    output = Path(output_dir).expanduser().resolve()
    static = _static_root()

    class Handler(BaseHTTPRequestHandler):
        server_version = "dreamy-web/1"

        def log_message(self, format: str, *args) -> None:
            return

        def _headers(self, content_type: str, length: int, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Security-Policy", _CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            if content_type == "application/json":
                self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def _error(self, status: int, code: str, message: str, head: bool = False) -> None:
            body = dumps({"error": {"code": code, "message": message}})
            self._headers("application/json", len(body), status)
            if not head:
                self.wfile.write(body)

        def _static(self, path: str, head: bool) -> bool:
            rel = path.removeprefix("/") or "index.html"
            if rel == "static" or rel.startswith("static/"):
                rel = rel.removeprefix("static/")
            if rel == "":
                rel = "index.html"
            parts = rel.split("/")
            if any(part in {"", ".", ".."} for part in parts):
                self._error(404, "not_found", "Resource not found", head)
                return True
            target = static.joinpath(*parts)
            if not target.is_file():
                return False
            data = target.read_bytes()
            content_type = "text/html; charset=utf-8" if target.suffix == ".html" else (
                "text/css; charset=utf-8" if target.suffix == ".css" else "application/javascript; charset=utf-8"
            )
            self._headers(content_type, len(data), 200)
            if not head:
                self.wfile.write(data)
            return True

        def _handle(self, head: bool = False) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/" or not parsed.path.startswith("/api/"):
                if self._static(parsed.path, head):
                    return
            if parsed.path.startswith("/api/") or parsed.path == "/healthz":
                store = None
                try:
                    db_path = output / "state.db"
                    if not db_path.is_file():
                        self._error(503, "state_unavailable", "State is unavailable", head)
                        return
                    store = ReadStore(db_path, output, read_only=True)
                    status, payload = dispatch(store, "HEAD" if head else "GET", parsed.path, parsed.query)
                    body = dumps(payload)
                    self._headers("application/json", len(body), status)
                    if not head:
                        self.wfile.write(body)
                except Exception:
                    self._error(503, "state_unavailable", "State is unavailable", head)
                finally:
                    if store is not None:
                        store.close()
                return
            self._error(404, "not_found", "Resource not found", head)

        def do_GET(self) -> None:
            self._handle(False)

        def do_HEAD(self) -> None:
            self._handle(True)

        def _method_not_allowed(self) -> None:
            # R17b/R17f: the stdlib's fallback for an unhandled verb is a 501
            # HTML page with no `Allow` header and no stable error code. Both
            # clauses require a 405 carrying `Allow` and the JSON error shape,
            # so every non-GET/HEAD verb is routed here rather than only the
            # four that happen to have `do_*` aliases.
            body = dumps({"error": {"code": "method_not_allowed", "message": "Method not allowed"}})
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Security-Policy", _CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        # `BaseHTTPRequestHandler` dispatches on `do_<VERB>`; anything without
        # an attribute yields its own 501. Catching the lookup itself is what
        # makes the guarantee total instead of a list we have to keep current.
        def __getattr__(self, name: str):
            if name.startswith("do_"):
                return self._method_not_allowed
            raise AttributeError(name)

    return ThreadingHTTPServer((host, port), Handler)


def serve(output_dir: Path | str, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = build_server(output_dir, host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
