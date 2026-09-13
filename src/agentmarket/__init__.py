"""AgentMarket：面向 AI Agent 的按次付费数据集市客户端。

本包只包含 Python SDK 和 CLI；服务端由部署方另行提供。
"""

from agentmarket.sdk import (
    AgentMarketError,
    BudgetExceededError,
    Client,
    Knowledge,
    KnowledgeResult,
    PaymentRequiredError,
)

__version__ = "0.2.0"

__all__ = [
    "AgentMarketError",
    "BudgetExceededError",
    "Client",
    "Knowledge",
    "KnowledgeResult",
    "PaymentRequiredError",
    "__version__",
]
