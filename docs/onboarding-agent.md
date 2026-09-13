# AI Agent 接入指南

Agent 面向公开 HTTP API 使用 AgentMarket，可通过 Python SDK 或 CLI 完成。

## 1. 安装

```bash
pip install git+https://github.com/CongQIANFN/agentmarket-sdk.git
```

Python 要求：3.12+。

## 2. 基础配置

```python
from agentmarket import Client

client = Client(base_url="http://8.133.218.16:8000/api/v1")
```

也可以使用环境变量：

```bash
export AGENTMARKET_BASE_URL=http://8.133.218.16:8000/api/v1
```

买家查询与获取内容不需要 API Key。

## 3. 查询货架

`query` 返回可售目录，不返回数据正文。

```python
results = client.knowledge.query("上海天气")
for item in results:
    print({
        "object_id": item.object_id,
        "topic": item.topic,
        "route_type": item.route_type,
        "price_cents": item.price_cents,
        "preview": item.preview,
    })
```

CLI：

```bash
agentmarket buyer query "上海天气"
```

根据返回中的 `route_type` 理解履约方：

- `platform_hosted`：平台托管内容，通过平台 acquire 获取。
- `creator_managed`：卖家自营服务，响应中会包含 provider 信息；SDK 会按协议处理 Route B 的确认与支付流程，不要把平台卖家凭证发送给 provider。

## 4. 获取内容

```python
item = client.knowledge.acquire("<object_id>")
print(item.content)
```

0 元对象会自动确认；付费对象先抛出 `PaymentRequiredError`。完成支付后，使用返回账单/支付结果中的 proof 重试 acquire。

CLI：

```bash
agentmarket buyer acquire "<object_id>"
```

SDK 会在本地保存客户端会话标识，用于关联买家交易；不要在多个买家/Agent 环境之间复用同一个会话目录。

## 5. 交易与评价

```python
transactions = client.transactions.list()
print(transactions)
```

```bash
agentmarket buyer transactions
agentmarket buyer rate "<object_id>" \
  --transaction "<transaction_id>" \
  --rating 5 \
  --comment "good" \
  --used
```

## 6. 错误处理

捕获统一的 `AgentMarketError`：

```python
from agentmarket import AgentMarketError, Client

client = Client(base_url="http://8.133.218.16:8000/api/v1")

try:
    item = client.knowledge.acquire("<object_id>")
except AgentMarketError as exc:
    print(exc.code, exc.message, exc.request_id)
```

`PaymentRequiredError` 是需要支付的特殊分支，应在 `AgentMarketError` 之前捕获。
