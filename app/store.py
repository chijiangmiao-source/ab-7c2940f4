"""持久化额度账本。

使用 SQLite 单库保存：

* ``meta``           —— 账本总额度（建账后不可变）；
* ``publications``   —— 每个稳定发布标识一行，持久化首份摘要/额度/状态/回执；
* ``outbox``         —— 待发给接收端的投递记录（含阶段），用于断电恢复。

关键语义：

* 以稳定发布标识幂等：相同标识 + 相同载荷重传返回原回执且不再扣额；
  摘要或额度不同则返回冲突（HTTP 409）。
* 额度扣减与行插入在同一事务内完成，且对同一标识用 ``INSERT ... ON CONFLICT``
  配合事务串行写入，保证并发相同提交最终只形成一条发布记录、一份回执、
  一次额度冻结。
* 已完成（committed）与未终结保留额（reserved：已冻结未完成）之和永不超过总额度。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

# 发布生命周期阶段
STAGE_ACCEPTED = "accepted"        # 已受理、额度已冻结，尚未向接收端交付
STAGE_DELIVERED = "delivered"      # 接收端已接受交付（持久化），本端尚未记账完成
STAGE_COMMITTED = "committed"      # 已完成：回执生成，冻结转为已用


class ConflictError(Exception):
    """同一发布标识以不同载荷（摘要或额度）重传。"""


class QuotaExceededError(Exception):
    """可用额度不足，无法冻结本次消耗。"""


class Store:
    """线程安全的 SQLite 账本封装。"""

    def __init__(self, db_path: str, total_quota: int) -> None:
        self.db_path = db_path
        self.total_quota = int(total_quota)
        self._lock = threading.Lock()
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._init_schema()

    # ------------------------------------------------------------------ schema
    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    total_quota INTEGER NOT NULL CHECK (total_quota >= 0)
                )
                """
            )
            # 建账时写入总额度；若库已存在则沿用既有总额度（不可变）。
            row = self._conn.execute("SELECT total_quota FROM meta WHERE id = 1").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta (id, total_quota) VALUES (1, ?)",
                    (self.total_quota,),
                )
            else:
                self.total_quota = row["total_quota"]

            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS publications (
                    publication_id TEXT PRIMARY KEY,
                    digest          TEXT NOT NULL,
                    amount          INTEGER NOT NULL CHECK (amount >= 0),
                    stage           TEXT NOT NULL,
                    receipt_id      TEXT,
                    delivered_count INTEGER NOT NULL DEFAULT 0,
                    created_at      REAL NOT NULL,
                    updated_at      REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox (
                    publication_id TEXT PRIMARY KEY,
                    digest          TEXT NOT NULL,
                    amount          INTEGER NOT NULL,
                    stage           TEXT NOT NULL,
                    created_at      REAL NOT NULL,
                    updated_at      REAL NOT NULL
                )
                """
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ----------------------------------------------------------------- helpers
    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "publication_id": row["publication_id"],
            "digest": row["digest"],
            "amount": row["amount"],
            "stage": row["stage"],
            "receipt_id": row["receipt_id"],
            "delivered_count": row["delivered_count"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def usage(self) -> dict[str, int]:
        """返回 committed / reserved / frozen / available 额度视图。"""
        with self._lock:
            agg = self._conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN stage = ? THEN amount END), 0) AS committed,
                    COALESCE(SUM(CASE WHEN stage != ? THEN amount END), 0) AS reserved
                FROM publications
                """,
                (STAGE_COMMITTED, STAGE_COMMITTED),
            ).fetchone()
        committed = int(agg["committed"])
        reserved = int(agg["reserved"])
        return {
            "total": self.total_quota,
            "committed": committed,
            "reserved": reserved,
            "frozen": reserved,
            "available": self.total_quota - committed - reserved,
        }

    def get(self, publication_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM publications WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def pending(self) -> list[dict[str, Any]]:
        """所有未终结（未 committed）的发布，按创建时间排序。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM publications WHERE stage != ? ORDER BY created_at",
                (STAGE_COMMITTED,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def list_publications(self) -> list[dict[str, Any]]:
        """全部发布记录（含阶段与回执），按创建时间排序。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM publications ORDER BY created_at"
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def receipts(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM publications WHERE receipt_id IS NOT NULL "
                "ORDER BY updated_at"
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ------------------------------------------------------------ core actions
    def reserve(self, publication_id: str, digest: str, amount: int) -> dict[str, Any]:
        """登记/重放一次提交，返回发布记录与是否为既有记录。

        * 既有且载荷一致 —— 直接返回原记录（不重复扣额）；
        * 既有但载荷不同 —— :class:`ConflictError`；
        * 新建 —— 在同一事务内校验额度并冻结，阶段 ``accepted``，写入 outbox。
        """
        amount = int(amount)
        now = time.time()
        with self._lock:
            # BEGIN IMMEDIATE 立即取写锁，串行化跨进程/跨线程的并发相同提交，
            # 使“额度校验 + 行插入”在单事务内原子完成。
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM publications WHERE publication_id = ?",
                    (publication_id,),
                ).fetchone()
                if row is not None:
                    if row["digest"] != digest or int(row["amount"]) != amount:
                        self._conn.execute("ROLLBACK")
                        raise ConflictError(self._row_to_dict(row))
                    self._conn.execute("COMMIT")
                    return self._row_to_dict(row)

                u = self.usage_locked()
                if amount > u["available"]:
                    self._conn.execute("ROLLBACK")
                    raise QuotaExceededError(
                        {"requested": amount, "available": u["available"]}
                    )
                self._conn.execute(
                    """
                    INSERT INTO publications
                        (publication_id, digest, amount, stage, receipt_id,
                         delivered_count, created_at, updated_at)
                    VALUES (?, ?, ?, ?, NULL, 0, ?, ?)
                    """,
                    (publication_id, digest, amount, STAGE_ACCEPTED, now, now),
                )
                self._conn.execute(
                    """
                    INSERT INTO outbox
                        (publication_id, digest, amount, stage, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (publication_id, digest, amount, STAGE_ACCEPTED, now, now),
                )
                self._conn.execute("COMMIT")
            except ConflictError:
                raise
            except QuotaExceededError:
                raise
            except sqlite3.IntegrityError:
                # 并发下另一进程已插入同一标识：回滚后按首份记录幂等返回/冲突。
                self._conn.execute("ROLLBACK")
                row = self._conn.execute(
                    "SELECT * FROM publications WHERE publication_id = ?",
                    (publication_id,),
                ).fetchone()
                if row is None:  # 极端情况：对端随后回滚
                    raise
                if row["digest"] != digest or int(row["amount"]) != amount:
                    raise ConflictError(self._row_to_dict(row))
                return self._row_to_dict(row)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            row = self._conn.execute(
                "SELECT * FROM publications WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
            return self._row_to_dict(row)

    def usage_locked(self) -> dict[str, int]:
        agg = self._conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN stage = ? THEN amount END), 0) AS committed,
                COALESCE(SUM(CASE WHEN stage != ? THEN amount END), 0) AS reserved
            FROM publications
            """ ,
            (STAGE_COMMITTED, STAGE_COMMITTED),
        ).fetchone()
        committed = int(agg["committed"])
        reserved = int(agg["reserved"])
        return {
            "total": self.total_quota,
            "committed": committed,
            "reserved": reserved,
            "frozen": reserved,
            "available": self.total_quota - committed - reserved,
        }

    def mark_delivered(self, publication_id: str) -> bool:
        """接收端已接受交付：仅在 accepted→delivered 真实转移时计数 +1。

        返回是否发生了转移；并发重放下只有一次调用会得到 True。
        """
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE publications SET stage = ?, delivered_count = delivered_count + 1, "
                "updated_at = ? WHERE publication_id = ? AND stage = ?",
                (STAGE_DELIVERED, now, publication_id, STAGE_ACCEPTED),
            )
            moved = cur.rowcount > 0
            self._conn.execute(
                "UPDATE outbox SET stage = ?, updated_at = ? WHERE publication_id = ?",
                (STAGE_DELIVERED, now, publication_id),
            )
            return moved

    def commit(self, publication_id: str, receipt_id: Optional[str] = None) -> dict[str, Any]:
        """收敛为原发布完成：采用接收端冻结回执、阶段 committed、移出 outbox。

        回执一旦确定便不可变更；恢复时按发布标识从接收端查回首份回执传入。
        """
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT * FROM publications WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
            if row is None:
                self._conn.execute("ROLLBACK")
                raise KeyError(publication_id)
            receipt_id = receipt_id or row["receipt_id"] or f"rcpt-{uuid.uuid4().hex}"
            self._conn.execute(
                "UPDATE publications SET stage = ?, receipt_id = ?, updated_at = ? "
                "WHERE publication_id = ?",
                (STAGE_COMMITTED, receipt_id, now, publication_id),
            )
            self._conn.execute(
                "DELETE FROM outbox WHERE publication_id = ?", (publication_id,)
            )
            self._conn.execute("COMMIT")
            row = self._conn.execute(
                "SELECT * FROM publications WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
            return self._row_to_dict(row)

    def reconcile(self, receiver: "ReceiverStore") -> list[str]:
        """断电恢复：按发布标识查询冻结回执并收敛为原发布完成。

        ``accepted`` 的记录：接收端没有交付记录时补投递一次（at-least-once，
        接收端自身幂等保证只计一次）；
        ``delivered`` 的记录：接收端已接受，直接按标识查回首份冻结回执。
        两种情况最终都以接收端首份回执提交为原发布完成，接收端交付次数仍为 1。
        返回被收敛的发布标识列表。
        """
        settled: list[str] = []
        for pub in self.pending():
            pid = pub["publication_id"]
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM publications WHERE publication_id = ?", (pid,)
                ).fetchone()
                if row["stage"] == STAGE_ACCEPTED:
                    # 接收端幂等：已存在则返回原记录，不新增交付次数。
                    recv_row = receiver.deliver(pid, row["digest"], int(row["amount"]))
                    self._conn.execute(
                        "UPDATE publications SET stage = ?, "
                        "delivered_count = delivered_count + 1, updated_at = ? "
                        "WHERE publication_id = ? AND stage = ?",
                        (STAGE_DELIVERED, time.time(), pid, STAGE_ACCEPTED),
                    )
                    self._conn.execute(
                        "UPDATE outbox SET stage = ?, updated_at = ? WHERE publication_id = ?",
                        (STAGE_DELIVERED, time.time(), pid),
                    )
                else:
                    recv_row = receiver.get(pid)
            receiver.wait_for_delivery(pid)
            if recv_row is None:
                recv_row = receiver.get(pid)
            self.commit(pid, receipt_id=recv_row["receipt_id"])
            settled.append(pid)
        return settled


class ReceiverStore:
    """接收端持久化存储：首份摘要与回执，按发布标识幂等。

    接收端独立落盘（模拟另一个可信进程/节点），重传同一标识的相同载荷
    返回首份回执，不新增交付次数；载荷不同则冲突。
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS deliveries (
                    publication_id TEXT PRIMARY KEY,
                    digest          TEXT NOT NULL,
                    amount          INTEGER NOT NULL,
                    receipt_id      TEXT NOT NULL,
                    created_at      REAL NOT NULL
                )
                """
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def deliver(self, publication_id: str, digest: str, amount: int) -> dict[str, Any]:
        """接收一次交付；相同标识+载荷重放返回首份记录，不重复计数。"""
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM deliveries WHERE publication_id = ?",
                    (publication_id,),
                ).fetchone()
                if row is not None:
                    if row["digest"] != digest or int(row["amount"]) != int(amount):
                        self._conn.execute("ROLLBACK")
                        raise ConflictError(dict(row))
                    self._conn.execute("COMMIT")
                    return dict(row)
                receipt_id = f"rcpt-{uuid.uuid4().hex}"
                self._conn.execute(
                    "INSERT INTO deliveries (publication_id, digest, amount, receipt_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (publication_id, digest, int(amount), receipt_id, now),
                )
                self._conn.execute("COMMIT")
            except ConflictError:
                raise
            except sqlite3.IntegrityError:
                self._conn.execute("ROLLBACK")
                row = self._conn.execute(
                    "SELECT * FROM deliveries WHERE publication_id = ?",
                    (publication_id,),
                ).fetchone()
                if row is None:
                    raise
                if row["digest"] != digest or int(row["amount"]) != int(amount):
                    raise ConflictError(dict(row))
                return dict(row)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            row = self._conn.execute(
                "SELECT * FROM deliveries WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
            return dict(row)

    def wait_for_delivery(self, publication_id: str) -> None:
        """接收端确认点：交付已在此前持久化即视为可见。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM deliveries WHERE publication_id = ?", (publication_id,)
            ).fetchone()
        if row is None:
            raise KeyError(publication_id)

    def get(self, publication_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM deliveries WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM deliveries ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    @property
    def delivered_count(self) -> int:
        with self._lock:
            (n,) = self._conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()
        return int(n)


def snapshot(store: Store, receiver: Optional[ReceiverStore] = None) -> dict[str, Any]:
    """页面/接口共用的完整状态快照。"""
    data: dict[str, Any] = {
        "quota": store.usage(),
        "publications": store.list_publications(),
    }
    if receiver is not None:
        data["receiver"] = {
            "delivered_count": receiver.delivered_count,
            "deliveries": receiver.all(),
        }
    return data
