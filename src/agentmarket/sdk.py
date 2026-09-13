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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("agentmarket.sdk")

DEFAULT_BASE_URL = "http://localhost:8000/api/v1"
SESSION_HEADER = "X-Client-Session"
PROOF_HEADER = "Payment-Proof"
ROUTE_B_TOKEN_HEADER = "X-AgentMarket-Route-B-Token"

# 客户端自造错误码独立区间（与服务端 03 文档第 5 节码位隔离，避免撞码）
CLIENT_ERROR_BASE = 90000

# 服务端 03 文档第 5 节：知识对象不存在/已下架 → 40401（与下方 40400/42201 同风格
# 直接写字面量，SDK 不反向依赖服务端模块，保持「主依赖只需 httpx」）
OBJECT_NOT_FOUND_CODE = 40401

# F14 恢复语义准入：只有确实付过钱的订单才算「有恢复资格」。pending/expired 未付款、
# refunded 已退款，都不构成绕过公开货架的理由（与服务端 buyer.RECOVERABLE_TX_STATUSES 同口径）
RECOVERABLE_TX_STATUSES = frozenset({"paid", "delivered"})


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
            message = (
                f"{message}：对象 {resource_id} 需支付 {amount} 元"
                "（bill 含完整账单，完成支付后凭 proof 重发 acquire）"
            )
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
    seller_name: str = ""
    seller_verified: bool = False
    value_type: str = ""
    price_cents: int = 0
    quality_score: float = 0.0
    preview: str = ""
    route_type: str = "platform_hosted"
    provider: dict | None = None
    call_count: int = 0
    payer_count: int = 0
    conversion_rate: float | None = None
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

    @property
    def creator_name(self) -> str:
        return self.seller_name

    @creator_name.setter
    def creator_name(self, value: str) -> None:
        self.seller_name = value

    @property
    def creator_verified(self) -> bool:
        return self.seller_verified

    @creator_verified.setter
    def creator_verified(self, value: bool) -> None:
        self.seller_verified = value

    @classmethod
    def from_api(cls, raw: dict) -> KnowledgeResult:
        seller = raw.get("seller") or raw.get("creator") or {}
        freshness = raw.get("freshness") or {}
        return cls(
            object_id=raw.get("object_id", ""),
            topic=raw.get("topic", ""),
            seller_name=seller.get("name", ""),
            seller_verified=bool(seller.get("verified")),
            value_type=raw.get("value_type", ""),
            price_cents=raw.get("price_cents", 0),
            quality_score=float(raw.get("quality_score") or 0.0),
            preview=raw.get("preview", ""),
            route_type=raw.get("route_type", "platform_hosted"),
            provider=raw.get("provider"),
            call_count=raw.get("call_count", 0),
            payer_count=int(raw.get("payer_count") or 0),
            conversion_rate=(
                float(raw["conversion_rate"]) if raw.get("conversion_rate") is not None else None
            ),
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
    seller: dict = field(default_factory=dict)
    freshness: dict = field(default_factory=dict)
    degraded: bool = False
    disclaimer: str = ""
    metadata: dict = field(default_factory=dict)
    content_version: int | None = None
    content_created_at: str | None = None
    metadata_status: str = "unavailable"
    _raw: dict = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        return _to_dict(self)

    @property
    def creator(self) -> dict:
        return self.seller

    @creator.setter
    def creator(self, value: dict) -> None:
        self.seller = value

    @classmethod
    def from_api(cls, raw: dict) -> Knowledge:
        return cls(
            object_id=raw.get("object_id", ""),
            content=raw.get("content") or {},
            seller=raw.get("seller") or raw.get("creator") or {},
            freshness=raw.get("freshness") or {},
            degraded=bool(raw.get("degraded")),
            disclaimer=raw.get("disclaimer", ""),
            metadata=raw.get("metadata") or {},
            content_version=raw.get("content_version"),
            content_created_at=raw.get("content_created_at"),
            metadata_status=raw.get("metadata_status", "unavailable"),
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

        下架/过期恢复（F14）：公开货架 `GET /knowledge/{object_id}` 返回 404 时，
        只要**带着 payment_proof** 或 **本会话已有该对象的已付订单**，acquire 仍会
        继续——路线A 由平台按 `paid_content_version` 交付购买时那个版本，路线B 从
        自己订单绑定的路线拿回 `provider.acquire_url` 后直连开发者 provider。
        两者都不具备时原样抛出 404（对象确实不存在，或本会话无权恢复）。
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
    def list(self, page: int = 1, page_size: int = 20, object_id: str | None = None) -> dict:
        """交易历史（自动携带本地持久化的会话）。

        object_id：可选过滤，按对象反查自己的订单（F14 恢复路径的准入证据）。
        """
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if object_id is not None:
            params["object_id"] = object_id
        return self._client._request("GET", "/transactions", params=params)

    def route(self, transaction_id: str) -> dict:
        """单笔订单绑定的路线元信息（03 文档第 3.5a 节 · F14 恢复通道）。

        对象下架后公开货架不再返回路线信息；本方法只能读**自己已付订单**
        （paid/delivered）当时绑定的路线，Route B 场景下即 `provider.acquire_url`，
        供本地 SDK 直连开发者 provider 完成恢复交付（平台不代理业务请求）。
        """
        return self._client._request("GET", f"/transactions/{transaction_id}/route")


class SellerAPI(_Namespace):
    """卖家管理方法（需 Client(api_key=..., seller_id=...)）。"""

    def publish_object(self, data: dict) -> dict:
        return self._client._request("POST", "/seller/objects", json=data)

    def update_object(self, object_id: str, data: dict) -> dict:
        return self._client._request("PUT", f"/seller/objects/{object_id}", json=data)

    def import_objects(self, data: dict, batch_id: str | None = None) -> dict:
        payload = dict(data)
        if batch_id:
            payload["batch_id"] = batch_id
        return self._client._request("POST", "/seller/objects/import", json=payload)

    def list_objects(self, page: int = 1, page_size: int = 20, status: str | None = None) -> dict:
        return self._client._request(
            "GET",
            "/seller/objects",
            params={"page": page, "page_size": page_size, **({"status": status} if status else {})},
        )

    def archive_object(self, object_id: str) -> dict:
        return self._client._request("POST", f"/seller/objects/{object_id}/archive")

    def register_endpoint(self, data: dict) -> dict:
        """注册 Route B provider/acquire 端点（03 文档第 4.4 节）。"""
        return self._client._request("POST", "/seller/endpoints/register", json=data)

    def sync_transactions(self, transactions: list[dict]) -> dict:
        """同步 Route B 最小交易元数据（03 文档第 4.4a 节）。"""
        return self._client._request(
            "POST", "/seller/transactions/sync", json={"transactions": transactions}
        )

    def dashboard(self, period: str = "30d") -> dict:
        """收益仪表盘（03 文档第 4.5 节）。"""
        return self._client._request("GET", "/seller/dashboard", params={"period": period})


CreatorAPI = SellerAPI


class _DeprecatedCreatorAPI(SellerAPI):
    """Deprecated alias: use client.seller.* instead of client.creator.*."""


class Client:
    """AgentMarket 买方/卖家客户端（07 文档第 1 节）。

    :param base_url: API 地址（默认读 AGENTMARKET_BASE_URL，未设则本机开发实例；
        与 MCP Server 的环境变量口径一致）
    :param api_key / seller_id: 卖家管理方法所需（买方方法无需）
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
        seller_id: str | None = None,
        creator_id: str | None = None,
        auto_pay: bool = False,
        max_price_per_call_cents: int | None = None,
        max_spending_per_day_cents: int | None = None,
        session_path: str | Path | None = None,
        timeout: float = 30.0,
        trust_env: bool = True,
        provider_allowed_hosts: list[str] | None = None,
        provider_payment_handler: Callable[[dict], str] | None = None,
        _transport: httpx.BaseTransport | None = None,  # 测试注入
    ):
        self.base_url = (
            base_url or os.environ.get("AGENTMARKET_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self.api_key = api_key
        self.seller_id = seller_id or creator_id
        self.auto_pay = auto_pay
        self.max_price_per_call_cents = max_price_per_call_cents
        self.max_spending_per_day_cents = max_spending_per_day_cents
        self.provider_allowed_hosts = provider_allowed_hosts or []
        self.provider_payment_handler = provider_payment_handler

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
        self.seller = SellerAPI(self)
        self.creator = _DeprecatedCreatorAPI(self)

    @property
    def creator_id(self) -> str | None:
        return self.seller_id

    @creator_id.setter
    def creator_id(self, value: str | None) -> None:
        self.seller_id = value

    def _provider_host_allowed(self, acquire_url: str) -> bool:
        """Route B provider 外呼白名单（不命中本地白名单就拒绝）。"""
        parsed = urlparse(acquire_url)
        hostname = parsed.hostname.lower() if parsed.hostname else ""
        if not hostname:
            return False
        return any(
            hostname == host.lower() or hostname.endswith(f".{host.lower()}")
            for host in self.provider_allowed_hosts
        )

    def _provider_url(self, acquire_url: str) -> str:
        """校验 Route B 外呼 URL 的 scheme/host（默认 fail-closed）。"""
        parsed = urlparse(acquire_url)
        hostname = parsed.hostname.lower() if parsed.hostname else ""
        if parsed.scheme != "https":
            if parsed.scheme != "http" or hostname not in ("localhost", "127.0.0.1", "::1"):
                raise AgentMarketError(
                    CLIENT_ERROR_BASE + 3,
                    "provider.acquire_url 必须使用 HTTPS（本地调试仅允许 localhost HTTP）",
                )
        if not self.provider_allowed_hosts:
            raise AgentMarketError(
                CLIENT_ERROR_BASE + 3,
                "Route B provider 未配置本地 provider_allowed_hosts，拒绝外呼",
            )
        if not self._provider_host_allowed(acquire_url):
            raise AgentMarketError(
                CLIENT_ERROR_BASE + 3, f"provider URL 未命中 provider_allowed_hosts: {hostname}"
            )
        return acquire_url

    def _check_budget(self, amount_cents: int, request_id: str = "") -> None:
        """所有会花钱路径共用的本地预算闸门（P3-2）。

        单次限额语义保持“本次账单金额 > max_price_per_call_cents 即拒绝”；
        日限额语义保持“今日已花 + 本次 > max_spending_per_day_cents 即拒绝”。
        """
        if self.max_price_per_call_cents is not None and (
            amount_cents > self.max_price_per_call_cents
        ):
            raise BudgetExceededError(
                CLIENT_ERROR_BASE + 2,
                f"单价 {amount_cents} 分超出单次限额 {self.max_price_per_call_cents} 分",
                request_id,
            )
        if self.max_spending_per_day_cents is not None:
            spent_today = self.session.spent_today()
            if spent_today + amount_cents > self.max_spending_per_day_cents:
                raise BudgetExceededError(
                    CLIENT_ERROR_BASE + 2,
                    f"日消费将超出限额 {self.max_spending_per_day_cents} 分"
                    f"（今日已用 {spent_today} 分）",
                    request_id,
                )

    def _mock_pay(self, amount_cents: int, out_trade_no: str, request_id: str) -> str:
        """路线A mock 支付唯一入口：先预算检查，成功后统一本地记账。"""
        self._check_budget(amount_cents, request_id)
        try:
            data = self._request("POST", "/payment/mock-pay", json={"out_trade_no": out_trade_no})
            proof = data["proof"]
        except AgentMarketError as pay_err:
            if pay_err.code == 40400:
                raise AgentMarketError(
                    40200, "付费对象需要真实支付宝支付（当前环境未接入）", request_id
                ) from pay_err
            raise
        self.session.add_spent(amount_cents)
        return proof

    def _pay_with_provider_handler(self, bill: dict, amount_cents: int, request_id: str) -> str:
        """Route B 付费 handler 唯一入口：先预算检查，再调用外部支付回调。"""
        self._check_budget(amount_cents, request_id)
        proof = self.provider_payment_handler(bill)
        self.session.add_spent(amount_cents)
        return proof

    def _issue_route_b_linkage_token(self, object_id: str) -> str:
        """Route B 外呼前申请新 linkage token（只保存在本次调用栈内）。"""
        data = self._request("POST", "/buyer/route-b/linkage-token", json={"object_id": object_id})
        token = data.get("linkage_token")
        if not isinstance(token, str) or not token:
            raise AgentMarketError(
                CLIENT_ERROR_BASE + 3, "平台未返回 Route B linkage token，拒绝外呼 provider"
            )
        return token

    def _request_external(
        self,
        method: str,
        url: str,
        *,
        json: dict | None = None,
        extra_headers: dict | None = None,
    ) -> httpx.Response:
        """向开发者自营 provider 发请求（不拼接平台 base_url，不携带平台鉴权）。"""
        headers = self._headers(extra_headers)
        headers.pop("Authorization", None)
        headers.pop("X-Creator-Id", None)
        # 🔴 sec-1 修复：X-Client-Session 即平台侧买方身份凭证（服务端 auth 以它识别买家），
        # 不能透传给第三方 provider（拿到即可冒名该买方调平台 API、抢占未交付订单）。
        headers.pop(SESSION_HEADER, None)
        try:
            return self._http.request(
                method, url, json=json, headers=headers, follow_redirects=False
            )
        except httpx.HTTPError as e:
            raise AgentMarketError(
                CLIENT_ERROR_BASE + 1, f"provider 网络请求失败: {type(e).__name__}: {e}"
            ) from e

    def _parse_provider(self, resp: httpx.Response) -> dict:
        # 🔴 sec-2 修复：第三方 provider 响应不得回写平台会话——否则 provider
        # 可通过 Payment-Needed/响应头里的恶意 X-Client-Session 把受害者 session
        # 固定成攻击者已知值（session fixation），窃取后续所有购买。
        try:
            body = resp.json()
        except ValueError:
            raise AgentMarketError(
                CLIENT_ERROR_BASE + resp.status_code,
                f"provider 返回非 JSON: {resp.status_code}",
            ) from None
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
                body.get("code", 40200),
                body.get("message", "Payment Required"),
                body.get("request_id", ""),
                bill,
            )
        if not (200 <= resp.status_code < 300):
            # bug-02 修复：provider 非 2xx（404/500/网关错等），即便 body 未带
            # code 字段也必须按失败处理——否则 body.get("code", 0) 默认 0 会把
            # 真正的错误体当成功内容返回。
            default_code = CLIENT_ERROR_BASE + resp.status_code
            if isinstance(body, dict):
                raise AgentMarketError(
                    body.get("code") or default_code,
                    body.get("message") or f"provider HTTP {resp.status_code}",
                    body.get("request_id", ""),
                )
            raise AgentMarketError(default_code, f"provider HTTP {resp.status_code}")
        if isinstance(body, dict) and body.get("code", 0) != 0:
            raise AgentMarketError(
                body.get("code", resp.status_code),
                body.get("message", "unknown"),
                body.get("request_id", ""),
            )
        return (body.get("data") or body) if isinstance(body, dict) else body

    # ---- 底层请求 ----

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {SESSION_HEADER: self.session.load_or_create()}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.seller_id:
            headers["X-Seller-Id"] = self.seller_id
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
        # 07 文档：SDK 对 Agent 保持统一 acquire 调用体验，但内部按目录 route_type 分流
        try:
            route = self.knowledge.get(object_id)
        except AgentMarketError as e:
            if e.code != OBJECT_NOT_FOUND_CODE:
                raise  # 非「对象不存在/已下架」的错误如实抛出，不做任何兜底
            # F14：公开货架 404 不再等于「无法交付」——已付过钱的调用者可以绕过
            # 货架走恢复通道（服务端仍按 Proof / 已付订单 fail-closed 裁决）
            return self._acquire_recovered(object_id, params, payment_proof, shelf_error=e)
        if route.route_type == "creator_managed":
            return self._acquire_route_b(route, params=params, payment_proof=payment_proof)
        return self._acquire_route_a(object_id, params, payment_proof)

    def _recovery_order(self, object_id: str) -> dict | None:
        """反查当前会话对该对象的已付订单（F14 恢复语义的准入证据之一）。

        列表按 created_at 倒序，取第一条 paid/delivered 即「最近一次真付过钱的
        订单」；没有则返回 None（pending/expired/refunded 都不构成恢复资格）。
        """
        data = self.transactions.list(object_id=object_id, page_size=100)
        for record in (data or {}).get("records", []):
            if record.get("status") in RECOVERABLE_TX_STATUSES:
                return record
        return None

    def _acquire_recovered(
        self,
        object_id: str,
        params: dict | None,
        payment_proof: str | None,
        *,
        shelf_error: AgentMarketError,
    ) -> Knowledge:
        """公开货架 404 后的恢复路径（F14）。

        只有恢复语义成立才绕过货架：**带 Payment-Proof** 或 **查到自己的已付订单**。
        两者都不成立 → 原样抛出货架 404（对象确实不存在、或本会话无权恢复），
        绝不一律吞掉 404。
        """
        order: dict | None = None
        try:
            order = self._recovery_order(object_id)
        except AgentMarketError as lookup_err:
            if not payment_proof:
                raise  # 无凭证时订单反查是唯一依据，它失败就不能假装能恢复
            # 带 Proof 时权威裁决在服务端六项校验（fail-closed）；订单反查只用来
            # 判断走哪条路线，它故障不该挡住一次已经付过钱的恢复
            logger.warning(
                "恢复路径订单反查失败，按路线A 直发 acquire 交服务端验签裁决：%s", lookup_err
            )
        if order is None and not payment_proof:
            raise shelf_error

        if order is not None:
            route = self.transactions.route(order["transaction_id"])
            if route.get("route_type") == "creator_managed":
                # 路线B 恢复：provider 元信息取自**自己已付订单**当时绑定的路线，
                # 而非当前公开货架；平台仍不代理业务请求，SDK 直连 acquire_url
                return self._acquire_route_b(
                    KnowledgeResult(
                        object_id=object_id,
                        topic=order.get("topic", ""),
                        route_type="creator_managed",
                        provider=route.get("provider"),
                    ),
                    params=params,
                    payment_proof=payment_proof,
                )
        # 路线A 恢复（含带 Proof 但反查不到订单的情形）：直发平台 acquire，服务端
        # 凭 Proof 或会话下的 paid 订单交付**购买时版本**；不成立则如实报错
        return self._acquire_route_a(object_id, params, payment_proof)

    def _acquire_route_a(
        self,
        object_id: str,
        params: dict | None = None,
        payment_proof: str | None = None,
    ) -> Knowledge:
        """路线A（platform_hosted + hosted）：平台 acquire 端点的 A402 时序。"""
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

            # 3. 付费走支付通道（V1 mock 通道 = /payment/mock-pay；prod 404 → 真实支付未接入）
            proof = self._mock_pay(amount_cents, out_trade_no, e.request_id)

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

    def _acquire_route_b(
        self,
        route: KnowledgeResult,
        *,
        params: dict | None,
        payment_proof: str | None,
    ) -> Knowledge:
        provider = route.provider or {}
        acquire_url = provider.get("acquire_url")
        if not acquire_url:
            raise AgentMarketError(
                CLIENT_ERROR_BASE + 3, "Route B 对象缺少 provider.acquire_url，拒绝调用"
            )
        acquire_url = self._provider_url(acquire_url)
        linkage_token = self._issue_route_b_linkage_token(route.object_id)
        linkage_headers = {ROUTE_B_TOKEN_HEADER: linkage_token}

        body: dict[str, Any] = {"object_id": route.object_id}
        if params is not None:
            body["params"] = params
        if payment_proof is not None:
            extra_headers = {**linkage_headers, PROOF_HEADER: payment_proof}
            resp = self._request_external(
                "POST", acquire_url, json=body, extra_headers=extra_headers
            )
            return Knowledge.from_api(self._parse_provider(resp))

        try:
            resp = self._request_external(
                "POST", acquire_url, json=body, extra_headers=linkage_headers
            )
            return Knowledge.from_api(self._parse_provider(resp))
        except PaymentRequiredError as e:
            bill = e.bill or {}
            protocol = bill.get("protocol") or {}
            amount_cents = yuan_to_cents(str(protocol.get("amount", "0")))
            out_trade_no = protocol.get("out_trade_no", "")

            if amount_cents == 0:
                zero_confirm_url = provider.get("zero_price_confirm_url")
                if not zero_confirm_url:
                    raise PaymentRequiredError(
                        e.code,
                        "Route B provider 未提供 zero_price_confirm_url，无法自动完成 0 元 mock",
                        e.request_id,
                        bill,
                    ) from e
                zero_confirm_url = self._provider_url(zero_confirm_url)
                confirm = self._request_external(
                    "POST",
                    zero_confirm_url,
                    json={
                        "out_trade_no": out_trade_no,
                        "resource_id": route.object_id,
                        "amount_cents": 0,
                    },
                    extra_headers=linkage_headers,
                )
                confirm_data = self._parse_provider(confirm)
                proof = confirm_data.get("proof") or confirm_data.get("payment_proof")
                if not proof:
                    raise AgentMarketError(
                        CLIENT_ERROR_BASE + 3,
                        "provider zero_price_confirm 响应缺少 payment_proof/proof",
                    ) from e
                resp = self._request_external(
                    "POST",
                    acquire_url,
                    json=body,
                    extra_headers={**linkage_headers, PROOF_HEADER: proof},
                )
                return Knowledge.from_api(self._parse_provider(resp))

            if not self.auto_pay:
                raise

            if self.provider_payment_handler is None:
                raise PaymentRequiredError(
                    e.code,
                    "Route B 付费对象需要 provider_payment_handler 或显式 payment_proof",
                    e.request_id,
                    bill,
                ) from e

            proof = self._pay_with_provider_handler(bill, amount_cents, e.request_id)
            resp = self._request_external(
                "POST",
                acquire_url,
                json=body,
                extra_headers={**linkage_headers, PROOF_HEADER: proof},
            )
            return Knowledge.from_api(self._parse_provider(resp))

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

        if amount_cents == 0:
            proof = self._confirm_with_retry_hint(body["object_id"], e.request_id)
        else:
            proof = self._mock_pay(amount_cents, out_trade_no, e.request_id)

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
