"""交付编排：本端账本与接收端之间的两阶段提交。

阶段推进：``accepted``（额度冻结）→ ``delivered``（接收端已接受并持久化回执）
→ ``committed``（本端记账完成，采用接收端首份回执）。

``crash_after_receive`` 用于模拟“接收后断电”：接收端已接受交付、本端记账
（commit）之前进程立即退出；重启时由 :meth:`LedgerService.recover` 收敛。
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional

from .store import (
    ReceiverStore,
    STAGE_ACCEPTED,
    STAGE_COMMITTED,
    Store,
    snapshot,
)

# 测试/演练用：接收端交付成功、本端提交前断电（立即退出，不做任何清理）。
CrashHook = Callable[[], None]


def default_crash_hook() -> None:
    # os._exit 不执行 finally/atexit/缓冲刷新，模拟硬断电。
    os._exit(1)


class LedgerService:
    def __init__(
        self,
        store: Store,
        receiver: ReceiverStore,
        crash_hook: Optional[CrashHook] = None,
    ) -> None:
        self.store = store
        self.receiver = receiver
        self._crash_hook = crash_hook or default_crash_hook

    # ----------------------------------------------------------------- runtime
    def state(self) -> dict[str, Any]:
        return snapshot(self.store, self.receiver)

    def get(self, publication_id: str) -> Optional[dict[str, Any]]:
        return self.store.get(publication_id)

    def submit(
        self,
        publication_id: str,
        digest: str,
        amount: int,
        crash_after_receive: bool = False,
    ) -> dict[str, Any]:
        """提交一次交付，返回最终发布记录（含回执）。

        * 新标识：原子冻结额度 → 接收端交付（持久化首份摘要与回执）→ 本端记账完成；
        * 相同标识相同载荷重传：返回原回执，不再扣额、不新增接收端交付；
        * 相同标识但摘要或额度不同：:class:`ConflictError`；
        * 可用额度不足：:class:`QuotaExceededError`。
        """
        amount = int(amount)
        # reserve 对既有相同载荷做幂等判定，对新载荷原子冻结额度。
        pub = self.store.reserve(publication_id, digest, amount)

        if pub["stage"] == STAGE_COMMITTED:
            # 重传：原发布已完成，直接返回原回执，不再扣额、不再投递。
            return pub

        if pub["stage"] == STAGE_ACCEPTED:
            recv_row = self.receiver.deliver(publication_id, digest, amount)
            self.store.mark_delivered(publication_id)
            if crash_after_receive:
                # 接收端已接受并持久化，本端尚未记账 —— 此刻断电。
                self._crash_hook()
        else:  # STAGE_DELIVERED：此前在提交前断电，进程内仍见未终结记录
            recv_row = self.receiver.get(publication_id)

        if recv_row is None:
            recv_row = self.receiver.get(publication_id)
        return self.store.commit(publication_id, receipt_id=recv_row["receipt_id"])

    def recover(self) -> list[str]:
        """启动恢复：按发布标识查询冻结回执并收敛为原发布完成。"""
        return self.store.reconcile(self.receiver)
