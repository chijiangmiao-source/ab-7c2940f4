#!/usr/bin/env python3
"""verify 第二阶段：API/HTTP 冒烟复核。

覆盖：
A. API 层：总额 10 提交 6 → 回执 + 可用 4；再提 5 拒绝且接收端无新回执；
   相同载荷重传返回原回执；改动摘要/额度冲突；不变量校验。
B. HTTP 层：健康响应、交付页面要素；跨连接并发相同提交只形成一条记录/一份回执/
   一次冻结。
C. 断电恢复：真实子进程“接收后断电”退出（os._exit），重开后按发布标识收敛，
   接收端交付次数不增加。

任一断言失败即以非 0 退出码结束。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.service import LedgerService  # noqa: E402
from app.store import (  # noqa: E402
    ConflictError,
    QuotaExceededError,
    ReceiverStore,
    Store,
)

HOST = "127.0.0.1"
PORT = int(os.environ.get("SMOKE_PORT", "18080"))
BASE = f"http://{HOST}:{PORT}"
# 可选：Compose 中已编排的交付服务；verify 会先对它做只读健康/页面探活。
EXTERNAL_BASE = os.environ.get("BASE_URL", "").rstrip("/")


def hr(title: str) -> None:
    print(f"\n=== {title} ===")


def http(method: str, path: str, payload=None, timeout=10):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def wait_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, port), timeout=1):
                return
        except OSError as exc:
            last = exc
            time.sleep(0.2)
    raise RuntimeError(f"port {port} not up: {last}")


def wait_health(port: int, timeout: float = 15.0) -> None:
    wait_port(port, timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            code, body = http("GET", "/healthz", timeout=2)
            if code == 200 and body.get("status") == "ok":
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError("healthz never became healthy")


def start_server(data_dir: str, auto_recover: bool = True) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "HOST": HOST,
        "PORT": str(PORT),
        "DATA_DIR": data_dir,
        "TOTAL_QUOTA": "10",
        "ALLOW_RESET": "1",
        "AUTO_RECOVER": "1" if auto_recover else "0",
        "PYTHONPATH": ROOT,
    })
    log = open(os.path.join(data_dir, "server.log"), "ab", buffering=0)
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.server"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    wait_health(PORT)
    return proc


# --------------------------------------------------------------------- Scenario
def probe_external() -> None:
    """对 Compose 已编排交付服务做只读探活（健康响应 + 页面要素）。"""
    if not EXTERNAL_BASE:
        return
    hr("0. 外部交付服务只读探活（Compose app）")
    deadline = time.time() + 30
    health = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(EXTERNAL_BASE + "/healthz", timeout=3) as r:
                health = (r.status, json.loads(r.read().decode()))
            if health[0] == 200:
                break
        except Exception:
            time.sleep(1)
    assert health and health[0] == 200 and health[1].get("status") == "ok", health
    with urllib.request.urlopen(EXTERNAL_BASE + "/", timeout=5) as r:
        page = r.read().decode()
    for tok in ("可用额度", "冻结额度", "发布阶段", "接收方回执"):
        assert tok in page, f"交付页面缺少 {tok}"
    print("外部服务", EXTERNAL_BASE, "/healthz=200，交付页面要素齐全")


def scenario_api(d: str) -> None:
    hr("A. API 层冒烟（额度拒绝 / 幂等 / 冲突 / 不变量）")
    store = Store(os.path.join(d, "ledger.db"), 10)
    recv = ReceiverStore(os.path.join(d, "receiver.db"))
    svc = LedgerService(store, recv)

    pub = svc.submit("pub-001", "sha256:aaa", 6)
    st = svc.state()
    assert pub["stage"] == "committed", pub
    assert st["quota"]["available"] == 4, st["quota"]
    assert st["receiver"]["delivered_count"] == 1
    assert len(st["publications"]) == 1
    first_receipt = pub["receipt_id"]
    print("提交消耗 6：一份回执", first_receipt, "，可用额度 4")

    try:
        svc.submit("pub-002", "sha256:bbb", 5)
        raise AssertionError("消耗 5 必须被拒绝")
    except QuotaExceededError:
        pass
    st = svc.state()
    assert st["quota"]["available"] == 4
    assert st["receiver"]["delivered_count"] == 1, "接收端不能新增回执"
    assert svc.get("pub-002") is None, "被拒绝的提交不得留下记录"
    print("再提交消耗 5：已拒绝，接收端交付次数仍为 1，无新回执")

    again = svc.submit("pub-001", "sha256:aaa", 6)
    assert again["receipt_id"] == first_receipt
    assert svc.state()["quota"]["available"] == 4
    print("相同载荷重传：返回原回执，未再次扣额")

    try:
        svc.submit("pub-001", "sha256:other", 6)
        raise AssertionError("改动摘要必须冲突")
    except ConflictError:
        pass
    try:
        svc.submit("pub-001", "sha256:aaa", 5)
        raise AssertionError("改动额度必须冲突")
    except ConflictError:
        pass
    assert svc.state()["receiver"]["delivered_count"] == 1
    print("改动摘要或额度：均返回冲突，未新增记录")

    q = st["quota"]
    assert q["committed"] + q["reserved"] <= q["total"]
    store.close()
    recv.close()


def scenario_http_and_concurrency(data_dir: str) -> subprocess.Popen:
    hr("B. HTTP 冒烟（健康/页面/拒绝/并发相同提交）")
    proc = start_server(data_dir, auto_recover=False)
    code, body = http("POST", "/admin/reset")
    assert code == 200, body

    code, _ = http("GET", "/healthz")
    assert code == 200
    with urllib.request.urlopen(BASE + "/", timeout=5) as resp:
        page = resp.read().decode("utf-8")
    for tok in ("可用额度", "冻结额度", "阶段", "接收方回执", "/api/submit"):
        assert tok in page, f"页面缺少 {tok}"
    print("健康响应 200，交付页面包含可用/冻结额度、阶段、回执")

    code, body = http("POST", "/api/submit",
                      {"publication_id": "pub-001", "digest": "sha256:aaa", "amount": 6})
    assert code == 200, body
    receipt = body["publication"]["receipt_id"]
    assert body["state"]["quota"]["available"] == 4
    print("HTTP 提交 6：200，回执", receipt, "可用 4")

    code, body = http("POST", "/api/submit",
                      {"publication_id": "pub-002", "digest": "sha256:bbb", "amount": 5})
    assert code == 402, (code, body)
    code, st = http("GET", "/api/state")
    assert st["receiver"]["delivered_count"] == 1 and len(st["publications"]) == 1
    print("HTTP 提交 5：402 拒绝，无新回执")

    code, body = http("POST", "/api/submit",
                      {"publication_id": "pub-001", "digest": "sha256:aaa", "amount": 6})
    assert code == 200 and body["publication"]["receipt_id"] == receipt
    code, body = http("POST", "/api/submit",
                      {"publication_id": "pub-001", "digest": "sha256:xxx", "amount": 6})
    assert code == 409, (code, body)
    print("HTTP 重传同载荷返回原回执；改动摘要 409 冲突")

    # 并发相同提交（独立连接）：只形成一条发布记录、一份回执、一次冻结。
    http("POST", "/admin/reset")
    n = 20

    def one(_):
        return http("POST", "/api/submit",
                    {"publication_id": "pub-parallel", "digest": "sha256:p", "amount": 6})

    with ThreadPoolExecutor(max_workers=n) as ex:
        outs = list(ex.map(one, range(n)))
    codes = [c for c, _ in outs]
    receipts = {b["publication"]["receipt_id"] for c, b in outs if c == 200}
    assert codes.count(200) == n, f"并发提交应全部幂等成功: {codes}"
    assert len(receipts) == 1, f"回执必须唯一: {receipts}"
    code, st = http("GET", "/api/state")
    assert len(st["publications"]) == 1, st["publications"]
    assert st["receiver"]["delivered_count"] == 1
    assert st["quota"]["committed"] == 6 and st["quota"]["available"] == 4
    print(f"{n} 个并发相同提交：一条发布记录、一份回执、接收端 1 次交付、可用 4")
    return proc


def scenario_crash_restart(data_dir: str, proc: subprocess.Popen) -> None:
    hr("C. 接收后断电 → 重开按发布标识收敛")
    # 重置为已知基线：6 已完成、可用 4。
    code, _ = http("POST", "/admin/reset")
    assert code == 200
    code, body = http("POST", "/api/submit",
                      {"publication_id": "pub-001", "digest": "sha256:aaa", "amount": 6})
    assert code == 200
    base_receipt = body["publication"]["receipt_id"]

    # 接收端接受交付（金额 3）、本端记账前硬断电，进程退出码为 1。
    try:
        http("POST", "/api/submit",
             {"publication_id": "pub-crash", "digest": "sha256:crash",
              "amount": 3, "crash_after_receive": True}, timeout=5)
        raise AssertionError("断电请求应当断连而非成功返回")
    except (urllib.error.URLError, ConnectionError, OSError):
        pass

    deadline = time.time() + 8
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    assert proc.poll() == 1, f"断电后进程应以码 1 退出，实际 {proc.poll()}"
    print("本端在接收后退出（exit=1）")

    # 断电窗口检查（直接读持久化库，不经过已死亡的服务进程）。
    chk_store = Store(os.path.join(data_dir, "ledger.db"), 10)
    chk_recv = ReceiverStore(os.path.join(data_dir, "receiver.db"))
    try:
        pubs = {p["publication_id"]: p for p in chk_store.list_publications()}
        assert pubs["pub-crash"]["stage"] == "delivered", pubs["pub-crash"]
        assert pubs["pub-crash"]["receipt_id"] is None
        assert chk_store.usage()["frozen"] == 3
        assert chk_recv.delivered_count == 2
        frozen = chk_recv.get("pub-crash")
        assert frozen is not None and frozen["digest"] == "sha256:crash"
        frozen_receipt = frozen["receipt_id"]
    finally:
        chk_store.close()
        chk_recv.close()
    print("断电窗口：接收端已持久化首份摘要与冻结回执，本端冻结 3、阶段 delivered")

    # 重开（AUTO_RECOVER=1）：按发布标识查询冻结回执并收敛为原发布完成。
    proc2 = start_server(data_dir, auto_recover=True)
    try:
        code, st = http("GET", "/api/state")
        pubs = {p["publication_id"]: p for p in st["publications"]}
        assert pubs["pub-crash"]["stage"] == "committed", pubs["pub-crash"]
        assert pubs["pub-crash"]["receipt_id"] == frozen_receipt, "应收敛为原回执"
        assert pubs["pub-001"]["receipt_id"] == base_receipt
        assert st["quota"]["committed"] == 9 and st["quota"]["frozen"] == 0
        assert st["quota"]["available"] == 1
        assert st["receiver"]["delivered_count"] == 2, "接收端交付次数不得增加"
        print("重开恢复：pub-crash 以原冻结回执完成，可用 1，接收端交付仍为 2 次")

        # 恢复可重入；恢复后重传返回同一回执。
        code, body = http("POST", "/api/recover")
        assert code == 200 and body["settled"] == []
        code, body = http("POST", "/api/submit",
                          {"publication_id": "pub-crash", "digest": "sha256:crash", "amount": 3})
        assert code == 200 and body["publication"]["receipt_id"] == frozen_receipt
        code, st = http("GET", "/api/state")
        assert st["receiver"]["delivered_count"] == 2
        assert st["quota"]["available"] == 1
        print("恢复重试与重复投递最终一致：一份回执、一次冻结")

        # 恢复后超额仍拒绝。
        code, body = http("POST", "/api/submit",
                          {"publication_id": "pub-x", "digest": "sha256:x", "amount": 2})
        assert code == 402, (code, body)
        assert http("GET", "/api/state")[1]["receiver"]["delivered_count"] == 2
        print("恢复后提交消耗 2（可用仅 1）：拒绝且无新回执")
    finally:
        proc2.terminate()
        try:
            proc2.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc2.kill()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="ledger-smoke-")
    api_dir = os.path.join(tmp, "api")
    http_dir = os.path.join(tmp, "http")
    os.makedirs(api_dir)
    os.makedirs(http_dir)
    proc = None
    try:
        probe_external()
        scenario_api(api_dir)
        proc = scenario_http_and_concurrency(http_dir)
        scenario_crash_restart(http_dir, proc)
        proc = None
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    hr("SMOKE OK — 全部 API/HTTP/断电恢复复核通过")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"\nSMOKE FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
