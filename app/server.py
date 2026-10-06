"""交付页面与 HTTP API（仅依赖 Python 标准库）。

路由：
* ``GET  /``                 交付页面（可用/冻结额度、发布阶段、接收方回执）
* ``GET  /healthz``          健康响应
* ``GET  /api/state``        完整状态快照（JSON）
* ``POST /api/submit``       提交摘要与消耗 ``{publication_id, digest, amount,
                             crash_after_receive?}``
* ``GET  /api/publication?publication_id=...``  按发布标识查询（含冻结回执）
* ``POST /api/recover``      手动触发断电恢复

启动时自动执行一次恢复：重开后按发布标识查询冻结回执并收敛为原发布完成。

环境变量：``HOST`` ``PORT`` ``TOTAL_QUOTA`` ``DATA_DIR`` ``AUTO_RECOVER``
``CRASH_AFTER_RECEIVE``。
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .service import LedgerService
from .store import (
    ConflictError,
    QuotaExceededError,
    ReceiverStore,
    Store,
)

HERE = os.path.dirname(os.path.abspath(__file__))


def _json_default(obj):
    return str(obj)


class Handler(BaseHTTPRequestHandler):
    server_version = "QuotaLedger/1.0"

    # ------------------------------------------------------------ utilities
    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode(
            "utf-8"
        )
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: bytes, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[http] %s - %s\n" % (self.address_string(), fmt % args))

    # ------------------------------------------------------------------ GET
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                with open(os.path.join(HERE, "index.html"), "rb") as fh:
                    self._send_html(fh.read())
            elif path == "/healthz":
                self._send_json(200, {"status": "ok"})
            elif path == "/api/state":
                self._send_json(200, self.server.service.state())
            elif path == "/api/publication":
                qs = parse_qs(parsed.query)
                pid = (qs.get("publication_id") or [""])[0]
                pub = self.server.service.get(pid)
                if pub is None:
                    self._send_json(404, {"error": "publication not found"})
                else:
                    self._send_json(200, {"publication": pub})
            else:
                self._send_json(404, {"error": "not found"})
        except Exception as exc:  # pragma: no cover - defensive
            self._send_json(500, {"error": str(exc)})

    # ----------------------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/recover":
            settled = self.server.service.recover()
            self._send_json(200, {"settled": settled, "state": self.server.service.state()})
            return
        if path == "/admin/reset":
            if not self.server.allow_reset:
                self._send_json(403, {"error": "reset disabled"})
                return
            self.server.reset_service()
            self._send_json(200, {"reset": True, "state": self.server.service.state()})
            return
        if path != "/api/submit":
            self._send_json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid JSON body"})
            return

        pid = str(data.get("publication_id") or "").strip()
        digest = str(data.get("digest") or "").strip()
        try:
            amount = int(data.get("amount"))
        except (TypeError, ValueError):
            self._send_json(400, {"error": "amount must be an integer"})
            return
        if not pid or not digest:
            self._send_json(400, {"error": "publication_id and digest are required"})
            return
        if amount < 0:
            self._send_json(400, {"error": "amount must be non-negative"})
            return

        crash = bool(data.get("crash_after_receive")) or self.server.force_crash
        try:
            pub = self.server.service.submit(
                pid, digest, amount, crash_after_receive=crash
            )
        except ConflictError as exc:
            self._send_json(
                409,
                {"error": "conflict: same publication_id with different digest/amount",
                 "existing": exc.args[0]},
            )
            return
        except QuotaExceededError as exc:
            self._send_json(
                402,
                {"error": "quota exceeded", "detail": exc.args[0]},
            )
            return
        self._send_json(200, {"publication": pub, "state": self.server.service.state()})


def build_server(host: str, port: int, total_quota: int, data_dir: str,
                 force_crash: bool, auto_recover: bool,
                 allow_reset: bool = False) -> ThreadingHTTPServer:
    os.makedirs(data_dir, exist_ok=True)
    store = Store(os.path.join(data_dir, "ledger.db"), total_quota)
    receiver = ReceiverStore(os.path.join(data_dir, "receiver.db"))
    service = LedgerService(store, receiver)

    if auto_recover:
        settled = service.recover()
        if settled:
            sys.stderr.write("[recover] settled publications: %s\n" % settled)

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.service = service  # type: ignore[attr-defined]
    httpd.force_crash = force_crash  # type: ignore[attr-defined]
    httpd.allow_reset = allow_reset  # type: ignore[attr-defined]
    httpd.data_dir = data_dir  # type: ignore[attr-defined]
    httpd.total_quota = total_quota  # type: ignore[attr-defined]

    def reset_service():
        old = httpd.service
        old.store.close()
        old.receiver.close()
        for name in ("ledger.db", "receiver.db"):
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(os.path.join(data_dir, name + suffix))
                except FileNotFoundError:
                    pass
        httpd.service = LedgerService(
            Store(os.path.join(data_dir, "ledger.db"), total_quota),
            ReceiverStore(os.path.join(data_dir, "receiver.db")),
        )

    httpd.reset_service = reset_service  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    return httpd


def main(argv: list[str] | None = None) -> int:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    total_quota = int(os.environ.get("TOTAL_QUOTA", "10"))
    data_dir = os.environ.get("DATA_DIR", "/data")
    auto_recover = os.environ.get("AUTO_RECOVER", "1") not in ("0", "false", "False")
    force_crash = os.environ.get("CRASH_AFTER_RECEIVE", "0") not in ("0", "false", "False")
    allow_reset = os.environ.get("ALLOW_RESET", "0") not in ("0", "false", "False")

    httpd = build_server(host, port, total_quota, data_dir, force_crash,
                         auto_recover, allow_reset)
    sys.stderr.write(
        f"[start] quota ledger on {host}:{port} total_quota={total_quota} "
        f"data_dir={data_dir} force_crash={force_crash}\n"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
