#!/usr/bin/env python3
"""交付页面“构建”步骤。

项目仅依赖 Python 标准库，无需打包器；本脚本：

1. 编译全部应用/测试/校验代码，提前暴露语法错误；
2. 校验交付页面 ``app/index.html`` 包含规格要求的持续展示要素
   （可用额度、冻结额度、发布阶段、接收方回执）及 API 钩子；
3. 输出构建清单到 ``dist/build-manifest.json``。

退出码非 0 即构建失败，供 Compose 的 verify 首先检查。
"""

from __future__ import annotations

import compileall
import hashlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(ROOT, "app", "index.html")

REQUIRED_TOKENS = [
    "可用额度",
    "冻结额度",
    "发布阶段",  # 表头语义（阶段列）
    "阶段",
    "接收方回执",
    "/api/state",
    "/api/submit",
    "delivered_count",
    "receipt_id",
]


def fail(msg: str) -> None:
    print(f"[build] FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    t0 = time.time()
    targets = ["app", "tests", "verify"]
    for t in targets:
        path = os.path.join(ROOT, t)
        ok = compileall.compile_dir(path, quiet=1, force=True)
        if not ok:
            fail(f"compilation failed in {t}/")
    print("[build] python modules compiled OK")

    if not os.path.isfile(PAGE):
        fail(f"page not found: {PAGE}")
    with open(PAGE, "rb") as fh:
        html = fh.read()
    text = html.decode("utf-8")
    missing = [tok for tok in REQUIRED_TOKENS if tok not in text]
    if missing:
        fail(f"page missing required elements: {missing}")
    print(f"[build] page OK ({len(html)} bytes), all required tokens present")

    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    manifest = {
        "status": "ok",
        "page": "app/index.html",
        "page_sha256": hashlib.sha256(html).hexdigest(),
        "page_bytes": len(html),
        "targets": targets,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }
    with open(os.path.join(ROOT, "dist", "build-manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    print("[build] BUILD OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
