# AgentMarket SDK

面向 AI Agent 的按次付费数据集市——Python SDK。仅依赖 `httpx`，无服务端代码。

## 安装

```bash
pip install git+https://github.com/CongQIANFN/agentmarket-sdk.git
```

## 快速开始（买家，无需注册）

```python
from agentmarket import Client

client = Client(base_url="http://8.133.218.16:8000/api/v1")

# 搜索知识
results = client.knowledge.query("贵州茅台 基本面")
for r in results:
    print(f"{r.topic} - {r.price_cents}分")

# 获取内容（0 元自动完成）
k = client.knowledge.acquire(results[0].object_id)
print(k.content)
```

## 文档

- [人类用户接入指南](docs/onboarding-human.md)
- [AI Agent 接入指南](docs/onboarding-agent.md)（含 MCP 协议）

## 服务端

本仓库仅包含 SDK 客户端，服务端地址由 `base_url` 参数指定。
