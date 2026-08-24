"""AgentMarket：面向 AI Agent 的按次付费数据集市。

服务端：agentmarket.main:app（FastAPI 单体）
SDK：from agentmarket import Client（07 文档第 1 节规范入口）
"""

from agentmarket.sdk import (
    AgentMarketError,
    BudgetExceededError,
    Client,
    Knowledge,
    KnowledgeResult,
    PaymentRequiredError,
)

__version__ = "0.1.0"

__all__ = [
    "AgentMarketError",
    "BudgetExceededError",
    "Client",
    "Knowledge",
    "KnowledgeResult",
    "PaymentRequiredError",
    "__version__",
]
