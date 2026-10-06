"""核心账本语义测试：额度、拒绝、幂等、冲突、并发、断电恢复。"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.service import LedgerService
from app.store import (
    ConflictError,
    QuotaExceededError,
    ReceiverStore,
    STAGE_COMMITTED,
    Store,
)


class CrashSimulated(Exception):
    """模拟接收后断电：取代真正的 os._exit。"""


class LedgerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.total = 10
        self.svc = self.new_service()

    def tearDown(self) -> None:
        self.svc.store.close()
        self.svc.receiver.close()
        self.tmp.cleanup()

    def new_service(self, total: int | None = None, crash: bool = False):
        store = Store(os.path.join(self.dir, "ledger.db"), total or self.total)
        receiver = ReceiverStore(os.path.join(self.dir, "receiver.db"))
        if crash:
            def hook():
                raise CrashSimulated()
            return LedgerService(store, receiver, crash_hook=hook), store, receiver
        return LedgerService(store, receiver)

    def assert_invariant(self, svc) -> None:
        q = svc.state()["quota"]
        self.assertLessEqual(q["committed"] + q["reserved"], q["total"])
        self.assertEqual(
            q["available"], q["total"] - q["committed"] - q["reserved"]
        )
        self.assertGreaterEqual(q["available"], 0)


class TestBasicQuota(LedgerTestBase):
    def test_submit_six_leaves_four_with_one_receipt(self):
        pub = self.svc.submit("pub-001", "sha256:aaa", 6)
        self.assertEqual(pub["stage"], STAGE_COMMITTED)
        self.assertTrue(pub["receipt_id"].startswith("rcpt-"))
        st = self.svc.state()
        self.assertEqual(st["quota"]["available"], 4)
        self.assertEqual(st["quota"]["committed"], 6)
        self.assertEqual(st["quota"]["frozen"], 0)
        # 一份回执、一次接收端交付
        self.assertEqual(len(st["publications"]), 1)
        self.assertEqual(st["receiver"]["delivered_count"], 1)
        # 本端回执即接收端首份冻结回执
        recv = st["receiver"]["deliveries"][0]
        self.assertEqual(recv["receipt_id"], pub["receipt_id"])
        self.assertEqual(recv["digest"], "sha256:aaa")
        self.assert_invariant(self.svc)

    def test_submit_five_after_completed_is_rejected_no_new_receipt(self):
        self.svc.submit("pub-001", "sha256:aaa", 6)
        with self.assertRaises(QuotaExceededError):
            self.svc.submit("pub-002", "sha256:bbb", 5)
        st = self.svc.state()
        self.assertEqual(st["quota"]["available"], 4)
        self.assertEqual(len(st["publications"]), 1)
        self.assertEqual(st["receiver"]["delivered_count"], 1)
        # 被拒绝的发布不得留下任何记录或冻结
        self.assertIsNone(self.svc.get("pub-002"))
        self.assert_invariant(self.svc)


class TestIdempotency(LedgerTestBase):
    def test_same_payload_returns_original_receipt_no_charge(self):
        first = self.svc.submit("pub-001", "sha256:aaa", 6)
        second = self.svc.submit("pub-001", "sha256:aaa", 6)
        third = self.svc.submit("pub-001", "sha256:aaa", 6)
        self.assertEqual(second["receipt_id"], first["receipt_id"])
        self.assertEqual(third["receipt_id"], first["receipt_id"])
        st = self.svc.state()
        self.assertEqual(len(st["publications"]), 1)
        self.assertEqual(st["quota"]["committed"], 6)
        self.assertEqual(st["quota"]["available"], 4)
        self.assertEqual(st["receiver"]["delivered_count"], 1)
        self.assertEqual(st["publications"][0]["delivered_count"], 1)

    def test_changed_digest_conflicts(self):
        self.svc.submit("pub-001", "sha256:aaa", 6)
        with self.assertRaises(ConflictError):
            self.svc.submit("pub-001", "sha256:DIFFERENT", 6)
        st = self.svc.state()
        self.assertEqual(st["quota"]["committed"], 6)
        self.assertEqual(st["receiver"]["delivered_count"], 1)

    def test_changed_amount_conflicts(self):
        self.svc.submit("pub-001", "sha256:aaa", 6)
        with self.assertRaises(ConflictError):
            self.svc.submit("pub-001", "sha256:aaa", 5)
        # 即使改动后额度足够也必须冲突，且不扣额
        st = self.svc.state()
        self.assertEqual(st["quota"]["available"], 4)
        self.assertEqual(st["receiver"]["delivered_count"], 1)


class TestCrashRecovery(LedgerTestBase):
    def test_crash_after_receive_then_reopen_reconciles_once(self):
        svc2, store2, receiver2 = self.new_service(crash=True)
        with self.assertRaises(CrashSimulated):
            svc2.submit("pub-001", "sha256:aaa", 6, crash_after_receive=True)

        # “断电”瞬间：接收端已接受（1 次），本端停在 delivered，未记账。
        mid = svc2.state()
        self.assertEqual(mid["receiver"]["delivered_count"], 1)
        self.assertEqual(mid["publications"][0]["stage"], "delivered")
        self.assertEqual(mid["quota"]["frozen"], 6)
        self.assertIsNone(mid["publications"][0]["receipt_id"])
        frozen_receipt = mid["receiver"]["deliveries"][0]["receipt_id"]
        store2.close()
        receiver2.close()

        # 重开：新服务实例按发布标识恢复并收敛为原发布完成。
        svc3 = self.new_service()  # 打开同一持久化目录；构造时不自动恢复
        settled = svc3.recover()
        self.assertEqual(settled, ["pub-001"])
        after = svc3.state()
        self.assertEqual(after["publications"][0]["stage"], STAGE_COMMITTED)
        self.assertEqual(after["publications"][0]["receipt_id"], frozen_receipt)
        self.assertEqual(after["quota"]["committed"], 6)
        self.assertEqual(after["quota"]["available"], 4)
        self.assertEqual(after["quota"]["frozen"], 0)
        self.assertEqual(after["receiver"]["delivered_count"], 1)

        # 恢复必须可重入：再次恢复不产生任何新交付/回执
        again = svc3.recover()
        self.assertEqual(again, [])
        self.assertEqual(svc3.state()["receiver"]["delivered_count"], 1)

        # 恢复后重传仍返回同一回执，不再扣额
        replay = svc3.submit("pub-001", "sha256:aaa", 6)
        self.assertEqual(replay["receipt_id"], frozen_receipt)
        self.assertEqual(svc3.state()["receiver"]["delivered_count"], 1)

    def test_crash_before_delivery_resends_once_on_recovery(self):
        # 极端情形：冻结后、接收端交付前断电（阶段 accepted）。
        svc2, store2, receiver2 = self.new_service(crash=True)
        # 直接制造 accepted 记录（尚未交付接收端）
        svc2.store.reserve("pub-009", "sha256:zzz", 3)
        store2.close()
        receiver2.close()

        svc3 = self.new_service()
        svc3.recover()
        st = svc3.state()
        self.assertEqual(st["publications"][0]["stage"], STAGE_COMMITTED)
        self.assertEqual(st["receiver"]["delivered_count"], 1)
        self.assertEqual(st["quota"]["committed"], 3)


class TestConcurrency(LedgerTestBase):
    def test_concurrent_identical_submits_yield_single_record(self):
        n = 16
        results: list = []
        errors: list = []

        def worker():
            s = Store(os.path.join(self.dir, "ledger.db"), self.total)
            r = ReceiverStore(os.path.join(self.dir, "receiver.db"))
            svc = LedgerService(s, r)
            try:
                results.append(svc.submit("pub-same", "sha256:aaa", 6))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                s.close()
                r.close()

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        receipts = {p["receipt_id"] for p in results}
        self.assertEqual(len(receipts), 1)
        check = self.new_service()
        st = check.state()
        self.assertEqual(len(st["publications"]), 1)
        self.assertEqual(st["receiver"]["delivered_count"], 1)
        self.assertEqual(st["quota"]["committed"], 6)
        self.assertEqual(st["quota"]["available"], 4)
        check.store.close()
        check.receiver.close()

    def test_concurrent_distinct_submits_never_overcommit(self):
        # 总额度 10，每笔 3：恰好 3 笔成功（9），其余拒绝。
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker(i):
            s = Store(os.path.join(self.dir, "ledger.db"), self.total)
            r = ReceiverStore(os.path.join(self.dir, "receiver.db"))
            svc = LedgerService(s, r)
            try:
                svc.submit(f"pub-{i}", f"sha256:{i}", 3)
            except QuotaExceededError as exc:
                with lock:
                    errors.append(exc)
            finally:
                s.close()
                r.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        check = self.new_service()
        st = check.state()
        self.assertEqual(st["quota"]["committed"], 9)
        self.assertEqual(st["quota"]["available"], 1)
        self.assertEqual(st["receiver"]["delivered_count"], 3)
        self.assertEqual(len(errors), 5)
        self.assertLessEqual(
            st["quota"]["committed"] + st["quota"]["reserved"], st["quota"]["total"]
        )
        check.store.close()
        check.receiver.close()


class TestPersistence(LedgerTestBase):
    def test_first_digest_and_receipt_survive_reopen(self):
        first = self.svc.submit("pub-001", "sha256:first", 6)
        self.svc.store.close()
        self.svc.receiver.close()

        svc2 = self.new_service()
        pub = svc2.get("pub-001")
        self.assertEqual(pub["digest"], "sha256:first")
        self.assertEqual(pub["receipt_id"], first["receipt_id"])
        recv = svc2.receiver.get("pub-001")
        self.assertEqual(recv["digest"], "sha256:first")
        self.assertEqual(recv["receipt_id"], first["receipt_id"])
        svc2.store.close()
        svc2.receiver.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
