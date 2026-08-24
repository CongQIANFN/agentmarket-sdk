"""AgentMarket Python SDK（07 文档第 1 节）。

买方无需注册、无需 api_key（api_key 仅创作者管理方法用）：

    from agentmarket import Client
    client = Client()  # base_url 默认读 AGENTMARKET_BASE_URL，否则 http://localhost:8000/api/v1
    results = client.knowledge.query("贵州茅台 基本面")
    k = client.knowledge.acquire(results[0].object_id)  # 0 元自动 confirm（对 Agent 无感）

核心机制：
- 会话管理（07-A）：X-Client-Session 本地持久化（~/.agentmarket/session，0600），
  402 回显平台会话时自动覆盖本地值
- acquire 内部时序（07-C）：402 → 解析账单 → 0 元无条件自动 confirm（07-C「对 Agent
  无感」）；付费分支由 auto_pay 门控（V1 走 mock-pay 通道）→ 带 Proof 重发 → 数据
- auto_pay 限额（P3-2）：本地建议性限额（单次/单日），与支付宝侧用户额度无关
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("agentmarket.sdk")

DEFAULT_BASE_URL = "http://localhost:8000/api/v1"
SESSION_HEADER = "X-Client-Session"
PROOF_HEADER = "Payment-Proof"

# 客户端自造错误码独立区间（与服务端 03 文档第 5 节码位隔离，避免撞码）
CLIENT_ERROR_BASE = 90000


# ---------- 异常 ----------


class AgentMarketError(Exception):
    """服务端统一错误（code/message/request_id，03 文档第 2/5 节）。"""

    def __init__(self, code: int, message: str, request_id: str = ""):
        self.code = code
        self.message = message
        self.request_id = request_id
        super().__init__(f"[{code}] {message} (request_id={request_id})")


class PaymentRequiredError(AgentMarketError):
    """402 需要支付（携带解析后的账单 bill，07-C 时序入口）。

    message 在服务端文案基础上拼入金额与对象，供 Agent 日志/用户提示直接引用。
    """

    def __init__(self, code: int, message: str, request_id: str, bill: dict | None):
        protocol = (bill or {}).get("protocol") or {}
        amount = protocol.get("amount")
        resource_id = protocol.get("resource_id")
        if amount is not None:
            message = f"{message}：对象 {resource_id} 需支付 {amount} 元（bill 含完整账单，完成支付后凭 proof 重发 acquire）"
        super().__init__(code, message, request_id)
        self.bill = bill


class BudgetExceededError(AgentMarketError):
    """超出 SDK 本地限额（P3-2：建议性限额，未发起支付）。"""


# ---------- 数据模型（07 文档快速开始中的属性访问风格）----------


def _to_dict(obj) -> dict:
    return {k: v for k, v in vars(obj).items() if not k.startswith("_")}


@dataclass
class KnowledgeResult:
    """工具目录条目（货架，无数据内容）。"""

    object_id: str
    topic: str
    creator_name: str = ""
    creator_verified: bool = False
    value_type: str = ""
    price_cents: int = 0
    quality_score: float = 0.0
    preview: str = ""
    call_count: int = 0
    conversion_rate: float = 0.0
    valid_until: str | None = None
    input_schema: dict | None = None
    description: str = ""  # 03 文档第 3.2 节精确查找的完整元信息（query 目录可为空）
    _raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_valid(self) -> bool:
        """内容未过期（valid_until 未过；realtime/streaming 恒有效）。"""
        if not self.valid_until:
            return True
        try:
            return datetime.fromisoformat(self.valid_until.replace("Z", "+00:00")) > datetime.now(
                UTC
            )
        except ValueError:
            return True

    def to_dict(self) -> dict:
        return _to_dict(self)

    @classmethod
    def from_api(cls, raw: dict) -> KnowledgeResult:
        creator = raw.get("creator") or {}
        freshness = raw.get("freshness") or {}
        return cls(
            object_id=raw.get("object_id", ""),
            topic=raw.get("topic", ""),
            creator_name=creator.get("name", ""),
            creator_verified=bool(creator.get("verified")),
            value_type=raw.get("value_type", ""),
            price_cents=raw.get("price_cents", 0),
            quality_score=float(raw.get("quality_score") or 0.0),
            preview=raw.get("preview", ""),
            call_count=raw.get("call_count", 0),
            conversion_rate=float(raw.get("conversion_rate") or 0.0),
            valid_until=freshness.get("valid_until"),
            input_schema=raw.get("input_schema"),
            description=raw.get("description", ""),
            _raw=raw,
        )


@dataclass
class Knowledge:
    """交付信封（03 文档第 3.4 节六字段）。"""

    object_id: str
    content: dict
    creator: dict = field(default_factory=dict)
    freshness: dict = field(default_factory=dict)
    degraded: bool = False
    disclaimer: str = ""
    _raw: dict = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        return _to_dict(self)

    @classmethod
    def from_api(cls, raw: dict) -> Knowledge:
        return cls(
            object_id=raw.get("object_id", ""),
            content=raw.get("content") or {},
            creator=raw.get("creator") or {},
            freshness=raw.get("freshness") or {},
            degraded=bool(raw.get("degraded")),
            disclaimer=raw.get("disclaimer", ""),
            _raw=raw,
        )


# ---------- 会话与本地限额存储（07-A / P3-2）----------


def _default_home() -> Path:
    return Path(os.environ.get("AGENTMARKET_HOME", Path.home() / ".agentmarket"))


class _SessionStore:
    """会话标识与日消费的本地持久化（让续交付/账单复用/评分防刷对 SDK 用户生效）。

    会话即买方身份（免 Proof 续交付凭证），文件以 0600 原子创建（O_CREAT 带权限，
    无「先 0644 后 chmod」的可读窗口）；损坏内容（无 cs_ 前缀）视为失效重建。
    """

    def __init__(self, home: Path | None = None):
        self.home = home or _default_home()
        self.session_file = self.home / "session"
        self.spending_file = self.home / "spending.json"
        self._session: str | None = None

    def load_or_create(self) -> str:
        if self._session:
            return self._session
        try:
            value = self.session_file.read_text().strip()
            if value.startswith("cs_"):  # 前缀校验：撕裂/污染内容不当作身份发出
                self._session = value
                return value
            if value:
                logger.warning("会话文件内容异常（无 cs_ 前缀），已重建：%s", self.session_file)
        except OSError:
            pass
        self._session = f"cs_sdk_{uuid.uuid4().hex}"
        self.save(self._session)
        return self._session

    def save(self, session: str) -> None:
        self._session = session
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            # O_CREAT 即带 0600：权限原子生效（P4 审查 P1-1），失败仅降级不静默
            fd = os.open(self.session_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(session)
        except OSError as e:
            logger.warning("会话持久化失败（退化为内存会话）：%s %s", self.session_file, e)

    # ---- 日消费（本地建议性限额，P3-2；并发多实例可能少记，限额语义本就为建议性）----
    def spent_today(self) -> int:
        try:
            data = json.loads(self.spending_file.read_text())
            if data.get("date") == datetime.now(UTC).date().isoformat():
                return int(data.get("spent_cents", 0))
        except (OSError, ValueError):
            pass
        return 0

    def add_spent(self, cents: int) -> None:
        today = datetime.now(UTC).date().isoformat()
        data = {"date": today, "spent_cents": self.spent_today() + cents}
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            # 原子替换：tmp + os.replace，并发写不再产生撕裂 JSON（P4 遗留兑现）
            tmp = self.spending_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.spending_file)
        except OSError:
            pass


def yuan_to_cents(amount: str) -> int:
    """元字符串 → int 分（NEW-12：纯整数运算，禁 float 参与金额）。

    仅接受服务端 cents_to_yuan 产出的两位小数非负格式；负数/非法格式直接拒绝。
    """
    if not isinstance(amount, str) or not amount:
        raise ValueError(f"非法金额字符串: {amount!r}")
    yuan, _, cents = amount.partition(".")
    if yuan.startswith("+"):
        yuan = yuan[1:]
    if (not yuan.isdigit()) or (cents and not cents.isdigit()):
        raise ValueError(f"非法金额字符串: {amount!r}")
    cents = (cents + "00")[:2]  # 两位小数，缺位补零
    return int(yuan) * 100 + int(cents)


# ---------- SDK 客户端 ----------


class _Namespace:
    def __init__(self, client: Client):
        self._client = client


class KnowledgeAPI(_Namespace):
    def query(self, query: str = "", **kwargs) -> list[KnowledgeResult]:
        """模糊搜索，返回工具目录列表（03 文档第 3.1 节）。"""
        body = {"query": query, **kwargs}
        params = {k: v for k, v in body.items() if v is not None}
        data = self._client._request("POST", "/knowledge/query", json=params)
        return [KnowledgeResult.from_api(r) for r in (data or {}).get("results", [])]

    def get(self, object_id: str) -> KnowledgeResult:
        """精确查找单个工具的完整元信息（03 文档第 3.2 节）。"""
        data = self._client._request("GET", f"/knowledge/{object_id}")
        return KnowledgeResult.from_api(data)

    def acquire(
        self,
        object_id: str,
        params: dict | None = None,
        payment_proof: str | None = None,
    ) -> Knowledge:
        """获取内容（03 文档第 3.4 节）：自动完成标准 A402（07-C 时序）。

        payment_proof：真实支付（支付宝 A2M）完成后由 Agent 支付基础设施
        返回的 Proof——直接携带重发 acquire 完成交付（真实付费路径入口；
        未提供时走 402 账单 → auto_pay/mock 通道时序）。
        """
        return self._client._acquire(object_id, params, payment_proof)

    def rating(
        self,
        object_id: str,
        transaction_id: str,
        rating: int,
        comment: str | None = None,
        used: bool | None = None,
    ) -> None:
        """交易评分（03 文档第 3.6 节；防刷：一会话一对象一次）。

        注：07 文档接口表签名为 rating(transaction_id, rating)；服务端路径需要
        object_id（/knowledge/{object_id}/rating），故 SDK 补齐显式参数。
        """
        body: dict[str, Any] = {"transaction_id": transaction_id, "rating": rating}
        if comment is not None:
            body["comment"] = comment
        if used is not None:
            body["used"] = used
        self._client._request("POST", f"/knowledge/{object_id}/rating", json=body)


class PaymentAPI(_Namespace):
    def confirm(self, object_id: str) -> str:
        """0 元对象签发 mock Proof（03 文档第 3.3 节；acquire 内部自动调用）。"""
        data = self._client._request("POST", "/payment/confirm", json={"object_id": object_id})
        return data["proof"]

    def refund(self, transaction_id: str, reason: str = "") -> dict:
        """申请退款（03 文档第 3.7 节）。

        服务端随真实支付阶段实现（当前未接入）；SDK 方法按契约就位，调用直接报未实现，
        避免裸 40400 对第三方调用方造成困惑。
        """
        raise NotImplementedError(
            "退款随真实支付阶段开放（03 文档第 3.7 节；当前为 V1 mock 期，无真实资金流）"
        )


class TransactionsAPI(_Namespace):
    def list(self, page: int = 1, page_size: int = 20) -> dict:
        """交易历史（自动携带本地持久化的会话）。"""
        return self._client._request(
            "GET", "/transactions", params={"page": page, "page_size": page_size}
        )


class CreatorAPI(_Namespace):
    """创作者管理方法（需 Client(api_key=..., creator_id=...)）。"""

    def publish_object(self, data: dict) -> dict:
        return self._client._request("POST", "/creator/objects", json=data)

    def update_object(self, object_id: str, data: dict) -> dict:
        return self._client._request("PUT", f"/creator/objects/{object_id}", json=data)

    def import_objects(self, data: dict, batch_id: str | None = None) -> dict:
        payload = dict(data)
        if batch_id:
            payload["batch_id"] = batch_id
        return self._client._request("POST", "/creator/objects/import", json=payload)

    def list_objects(self, page: int = 1, page_size: int = 20, status: str | None = None) -> dict:
        return self._client._request(
            "GET",
            "/creator/objects",
            params={"page": page, "page_size": page_size, **({"status": status} if status else {})},
        )

    def archive_object(self, object_id: str) -> dict:
        return self._client._request("POST", f"/creator/objects/{object_id}/archive")

    def register_endpoint(self, data: dict) -> dict:
        """注册 External Endpoint（03 文档第 4.4 节；服务端随 external 通道开放后可用）。"""
        raise NotImplementedError(
            "External Endpoint 注册随 external 通道开放（03 文档第 4.4 节；当前服务端未实现）"
        )

    def dashboard(self, period: str = "30d") -> dict:
        """收益仪表盘（03 文档第 4.5 节；服务端随创作者后台开放后可用）。"""
        raise NotImplementedError(
            "创作者仪表盘随创作者后台开放（03 文档第 4.5 节；当前服务端未实现）"
        )


class Client:
    """AgentMarket 买方/创作者客户端（07 文档第 1 节）。

    :param base_url: API 地址（默认读 AGENTMARKET_BASE_URL，未设则本机开发实例；
        与 MCP Server 的环境变量口径一致）
    :param api_key / creator_id: 创作者管理方法所需（买方方法无需）
    :param auto_pay: 付费对象的自动支付开关（0 元 confirm 分支无条件自动完成，
        07-C「对 Agent 无感」）；False 时付费 402 抛 PaymentRequiredError
    :param max_price_per_call_cents: 单次购买上限（分，本地建议性限额 P3-2）
    :param max_spending_per_day_cents: 日消费上限（分）
    :param session_path: 会话持久化路径（默认 ~/.agentmarket/session）
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        api_key: str | None = None,
        creator_id: str | None = None,
        auto_pay: bool = False,
        max_price_per_call_cents: int | None = None,
        max_spending_per_day_cents: int | None = None,
        session_path: str | Path | None = None,
        timeout: float = 30.0,
        trust_env: bool = True,
        _transport: httpx.BaseTransport | None = None,  # 测试注入
    ):
        self.base_url = (
            base_url or os.environ.get("AGENTMARKET_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self.api_key = api_key
        self.creator_id = creator_id
        self.auto_pay = auto_pay
        self.max_price_per_call_cents = max_price_per_call_cents
        self.max_spending_per_day_cents = max_spending_per_day_cents

        if session_path is not None:
            # 指定会话文件：store 的 home 跟随该文件目录（spending.json 同目录）
            path = Path(session_path)
            self.session = _SessionStore(path.parent if str(path.parent) != "" else Path("."))
            self.session.session_file = path
        else:
            self.session = _SessionStore()
        self.session.load_or_create()

        self._http = httpx.Client(
            base_url=self.base_url, timeout=timeout, transport=_transport, trust_env=trust_env
        )
        self.knowledge = KnowledgeAPI(self)
        self.payment = PaymentAPI(self)
        self.transactions = TransactionsAPI(self)
        self.creator = CreatorAPI(self)

    # ---- 底层请求 ----

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {SESSION_HEADER: self.session.load_or_create()}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.creator_id:
            headers["X-Creator-Id"] = self.creator_id
        if extra:
            headers.update(extra)
        return headers

    def _sync_session_from_response(self, response: httpx.Response) -> None:
        """07-A：402 回显平台会话（首次未带时平台生成）→ 覆盖本地值。"""
        echoed = response.headers.get(SESSION_HEADER)
        if echoed and echoed != self.session.load_or_create():
            self.session.save(echoed)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        extra_headers: dict | None = None,
    ) -> dict:
        try:
            resp = self._http.request(
                method, path, json=json, params=params, headers=self._headers(extra_headers)
            )
        except httpx.HTTPError as e:
            # 传输层异常（超时/连接失败/代理断开）纳入统一异常体系（P4 遗留兑现）
            raise AgentMarketError(
                CLIENT_ERROR_BASE + 1, f"网络请求失败: {type(e).__name__}: {e}"
            ) from e
        self._sync_session_from_response(resp)
        return self._parse(resp)

    def _parse(self, resp: httpx.Response) -> dict:
        try:
            body = resp.json()
        except ValueError:
            # 客户端自造码段 9xxxx（与服务端 03 文档第 5 节码位隔离，防撞码）
            raise AgentMarketError(
                CLIENT_ERROR_BASE + resp.status_code, f"非 JSON 响应: {resp.status_code}"
            ) from None
        code = body.get("code", resp.status_code)
        if resp.status_code == 402:
            bill = None
            needed = resp.headers.get("Payment-Needed")
            if needed:
                import base64

                try:
                    bill = json.loads(base64.urlsafe_b64decode(needed))
                except Exception:
                    bill = None
            raise PaymentRequiredError(
                code, body.get("message", "Payment Required"), body.get("request_id", ""), bill
            )
        if code != 0:
            raise AgentMarketError(code, body.get("message", "unknown"), body.get("request_id", ""))
        return body.get("data") or {}

    # ---- acquire 自动支付时序（07-C）----

    def _acquire(
        self,
        object_id: str,
        params: dict | None = None,
        payment_proof: str | None = None,
    ) -> Knowledge:
        body: dict[str, Any] = {"object_id": object_id}
        if params is not None:
            body["params"] = params
        # 真实支付路径：Agent 已持 Proof → 首次请求即携带（跳过 402 时序）
        proof_header = {PROOF_HEADER: payment_proof} if payment_proof else None

        # 1. 无凭证请求（带会话）
        try:
            data = self._request(
                "POST", "/knowledge/acquire", json=body, extra_headers=proof_header
            )
            return Knowledge.from_api(data)  # 续交付直接成功（03-D）
        except PaymentRequiredError as e:
            bill = e.bill or {}
            protocol = bill.get("protocol") or {}
            amount_cents = yuan_to_cents(str(protocol.get("amount", "0")))
            out_trade_no = protocol.get("out_trade_no", "")

            # 0 元分支无条件自动 confirm（07-C「对 Agent 无感」，不受 auto_pay 门控）
            if amount_cents == 0:
                proof = self._confirm_with_retry_hint(object_id, e.request_id)
                data = self._request(
                    "POST", "/knowledge/acquire", json=body, extra_headers={PROOF_HEADER: proof}
                )
                return Knowledge.from_api(data)

            # ---- 付费分支：由 auto_pay 门控 ----
            if not self.auto_pay:
                raise

            # 2. 本地建议性限额（P3-2）：超限拒绝且不发起支付
            if (
                self.max_price_per_call_cents is not None
                and amount_cents > self.max_price_per_call_cents
            ):
                raise BudgetExceededError(
                    CLIENT_ERROR_BASE + 2,
                    f"单价 {amount_cents} 分超出单次限额 {self.max_price_per_call_cents} 分",
                    e.request_id,
                ) from e
            if self.max_spending_per_day_cents is not None:
                if self.session.spent_today() + amount_cents > self.max_spending_per_day_cents:
                    raise BudgetExceededError(
                        CLIENT_ERROR_BASE + 2,
                        f"日消费将超出限额 {self.max_spending_per_day_cents} 分"
                        f"（今日已用 {self.session.spent_today()} 分）",
                        e.request_id,
                    ) from e

            # 3. 付费走支付通道（V1 mock 通道 = /payment/mock-pay；prod 404 → 真实支付未接入）
            try:
                data = self._request(
                    "POST", "/payment/mock-pay", json={"out_trade_no": out_trade_no}
                )
                proof = data["proof"]
            except AgentMarketError as pay_err:
                if pay_err.code == 40400:
                    raise AgentMarketError(
                        40200, "付费对象需要真实支付宝支付（当前环境未接入）", e.request_id
                    ) from pay_err
                raise
            self.session.add_spent(amount_cents)

            # 4. 带 Proof 重发 acquire → 数据（402 = 服务端重签了账单，单次重试支付+重发）
            try:
                data = self._request(
                    "POST", "/knowledge/acquire", json=body, extra_headers={PROOF_HEADER: proof}
                )
                return Knowledge.from_api(data)
            except PaymentRequiredError as retry_e:
                # 服务端 BILL_EXPIRED 会重签新账单返回 402（04 文档 2.5 边界规则②）：
                # 用新账单走一次完整支付+重发（限一次，不无限循环）
                if self.auto_pay:
                    return self._acquire_with_bill(retry_e, body)
                raise

    def _confirm_with_retry_hint(self, object_id: str, request_id: str) -> str:
        """0 元 confirm；账单刚被清理时给出自愈指引而非误导性文案（P4 审查 P3-5）。"""
        try:
            return self.payment.confirm(object_id)
        except AgentMarketError as e:
            if e.code == 42201 and "未找到待支付账单" in e.message:
                raise AgentMarketError(
                    e.code, "账单已过期，请重试 acquire（将自动签发新账单）", request_id
                ) from e
            raise

    def _acquire_with_bill(self, e: PaymentRequiredError, body: dict) -> Knowledge:
        """服务端重签账单后的单次完整重试（07-C 从步骤 2 起重走）。"""
        bill = e.bill or {}
        protocol = bill.get("protocol") or {}
        amount_cents = yuan_to_cents(str(protocol.get("amount", "0")))
        out_trade_no = protocol.get("out_trade_no", "")

        if (
            self.max_price_per_call_cents is not None
            and amount_cents > self.max_price_per_call_cents
        ):
            raise BudgetExceededError(
                CLIENT_ERROR_BASE + 2,
                f"单价 {amount_cents} 分超出单次限额 {self.max_price_per_call_cents} 分",
                e.request_id,
            ) from e

        if amount_cents == 0:
            proof = self._confirm_with_retry_hint(body["object_id"], e.request_id)
        else:
            data = self._request("POST", "/payment/mock-pay", json={"out_trade_no": out_trade_no})
            proof = data["proof"]
            self.session.add_spent(amount_cents)

        data = self._request(
            "POST", "/knowledge/acquire", json=body, extra_headers={PROOF_HEADER: proof}
        )
        return Knowledge.from_api(data)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
