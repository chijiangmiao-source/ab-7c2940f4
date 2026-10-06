#!/usr/bin/env python3
"""Compose verify 容器入口。

顺序遵循规格：
1. 先“构建交付页面并检查结果”（build.py，失败即终止）；
2. 构建通过后，再以 API/HTTP 冒烟复核，同时运行代码测试；
3. 全部完成即退出，并以退出码报告结果（0 成功，非 0 失败）。
"""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(label: str, cmd: list[str]) -> int:
    print(f"\n########## [{label}] START: {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(
        cmd, cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(f"[{label}] {line}", end="", flush=True)
    proc.wait()
    print(f"########## [{label}] EXIT {proc.returncode}", flush=True)
    return proc.returncode


def main() -> int:
    # 1) 先构建交付页面并检查结果
    build_rc = run("build", [sys.executable, "build.py"])
    if build_rc != 0:
        print("\nVERIFY FAIL: 构建交付页面失败，终止后续复核", file=sys.stderr)
        return build_rc

    # 2) 构建通过：冒烟复核 与 代码测试 同时运行
    smoke_cmd = [sys.executable, "-m", "verify.smoke"]
    test_cmd = [sys.executable, "-m", "unittest", "discover",
                "-s", "tests", "-p", "test_*.py", "-v"]

    smoke = subprocess.Popen(smoke_cmd, cwd=ROOT,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    tests = subprocess.Popen(test_cmd, cwd=ROOT,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def pump(label, proc):
        assert proc.stdout is not None
        for line in proc.stdout:
            print(f"[{label}] {line}", end="", flush=True)

    # 用线程并行转发两个子进程的输出，避免任一管道写满阻塞。
    import threading
    t1 = threading.Thread(target=pump, args=("smoke", smoke), daemon=True)
    t2 = threading.Thread(target=pump, args=("tests", tests), daemon=True)
    t1.start(); t2.start()
    smoke_rc = smoke.wait()
    tests_rc = tests.wait()
    t1.join(); t2.join()

    print(f"\n########## smoke EXIT {smoke_rc} | tests EXIT {tests_rc}", flush=True)
    if smoke_rc == 0 and tests_rc == 0:
        print("VERIFY OK: 构建、API/HTTP 冒烟复核、代码测试全部通过")
        return 0
    failed = []
    if smoke_rc != 0:
        failed.append(f"smoke={smoke_rc}")
    if tests_rc != 0:
        failed.append(f"tests={tests_rc}")
    print(f"VERIFY FAIL: {', '.join(failed)}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
