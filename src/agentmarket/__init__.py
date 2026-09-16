"""AgentMarket：面向 AI Agent 的按次付费数据集市客户端。

本包只包含 Python SDK 和 CLI；服务端由部署方另行提供。
"""

from agentmarket.sdk import (
    SDK_VERSION,
    AgentMarketError,
    BudgetExceededError,
    Client,
    Knowledge,
    KnowledgeResult,
    PaymentRequiredError,
)

__version__ = SDK_VERSION

__all__ = [
    "AgentMarketError",
    "BudgetExceededError",
    "Client",
    "Knowledge",
    "KnowledgeResult",
    "PaymentRequiredError",
    "SDK_VERSION",
    "__version__",
]
