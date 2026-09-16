"""本地购买历史账本与付费重复购买闸门。"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentmarket.sdk import AgentMarketError

if os.name == "nt":
    import msvcrt
else:
    import fcntl


LEDGER_SCHEMA_VERSION = 1
REPURCHASE_BLOCKED_CODE = 91001
LEDGER_CORRUPTED_CODE = 91002


def default_home() -> Path:
    return Path(os.environ.get("AGENTMARKET_HOME", Path.home() / ".agentmarket"))


class PurchaseHistory:
    """跨进程安全的 JSONL 购买历史。

    读取失败不清理、不重建账本；付费 acquire 在闸门阶段遇到损坏时 fail-closed。
    """

    def __init__(self, home: Path | None = None, *, base_url: str, session: str):
        self.home = home or default_home()
        self.ledger_path = self.home / "purchases.jsonl"
        self.lock_path = self.home / ".purchases.lock"
        self.base_url = base_url.rstrip("/")
        self.session = session

    @contextmanager
    def _lock(self):
        self.home.mkdir(parents=True, exist_ok=True)
        try:
            self.home.chmod(0o700)
        except OSError:
            pass
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _read_unlocked(self) -> list[dict[str, Any]]:
        try:
            raw = self.ledger_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise AgentMarketError(
                LEDGER_CORRUPTED_CODE, f"购买历史账本读取失败：{self.ledger_path}"
            ) from exc

        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AgentMarketError(
                    LEDGER_CORRUPTED_CODE,
                    f"购买历史账本第 {line_number} 行损坏，付费重复购买已停止：{self.ledger_path}",
                ) from exc
            if not isinstance(record, dict):
                raise AgentMarketError(
                    LEDGER_CORRUPTED_CODE,
                    f"购买历史账本第 {line_number} 行不是对象，付费重复购买已停止：{self.ledger_path}",
                )
            records.append(record)
        return records

    def records(self) -> list[dict[str, Any]]:
        with self._lock():
            return self._read_unlocked()

    def _match(self, record: dict[str, Any], object_id: str) -> bool:
        return (
            record.get("ledger_schema_version") == LEDGER_SCHEMA_VERSION
            and record.get("outcome") == "delivered"
            and record.get("base_url") == self.base_url
            and record.get("session") == self.session
            and record.get("object_id") == object_id
        )

    def find_delivered(self, object_id: str) -> dict[str, Any] | None:
        records = self.records()
        for record in reversed(records):
            if self._match(record, object_id):
                return record
        return None

    def require_repurchase_allowed(self, object_id: str, *, repurchase: bool) -> None:
        if repurchase:
            return
        record = self.find_delivered(object_id)
        if record is None:
            return
        purchased_at = record.get("ts", "未知时间")
        route_type = record.get("route_type", "未知路线")
        amount_cents = record.get("amount_cents", "未知金额")
        raise AgentMarketError(
            REPURCHASE_BLOCKED_CODE,
            f"对象 {object_id} 已于 {purchased_at} 购买交付（路线 {route_type}，"
            f"金额 {amount_cents} 分）。如需再次购买，请使用 repurchase=True 或 CLI --repurchase。",
        )

    def record(
        self,
        *,
        object_id: str,
        topic: str,
        route_type: str,
        amount_cents: int,
        transaction_id: str,
        out_trade_no: str,
        content_version: int | str | None,
        sdk_version: str,
    ) -> None:
        record = {
            "ledger_schema_version": LEDGER_SCHEMA_VERSION,
            "sdk_version": sdk_version,
            "ts": datetime.now(UTC).isoformat(),
            "base_url": self.base_url,
            "session": self.session,
            "object_id": object_id,
            "topic": topic,
            "route_type": route_type,
            "outcome": "delivered",
            "amount_cents": amount_cents,
            "transaction_id": transaction_id,
            "out_trade_no": out_trade_no,
            "content_version": content_version,
        }
        with self._lock():
            try:
                fd = os.open(self.ledger_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            except OSError as exc:
                raise AgentMarketError(
                    LEDGER_CORRUPTED_CODE, f"购买历史账本打开失败：{self.ledger_path}"
                ) from exc
            try:
                with os.fdopen(fd, "a", encoding="utf-8") as ledger:
                    ledger.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                    ledger.write("\n")
                    ledger.flush()
                    os.fsync(ledger.fileno())
            except OSError as exc:
                raise AgentMarketError(
                    LEDGER_CORRUPTED_CODE, f"购买历史账本写入失败：{self.ledger_path}"
                ) from exc

    def append(self, record: dict[str, Any]) -> None:
        """供并发测试写入完整记录；仍使用同一跨进程锁。"""
        with self._lock():
            try:
                fd = os.open(self.ledger_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as ledger:
                    ledger.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                    ledger.write("\n")
                    ledger.flush()
                    os.fsync(ledger.fileno())
            except OSError as exc:
                raise AgentMarketError(
                    LEDGER_CORRUPTED_CODE, f"购买历史账本写入失败：{self.ledger_path}"
                ) from exc
