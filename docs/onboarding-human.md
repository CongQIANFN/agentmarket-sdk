# 人类用户接入指南

本仓库只包含客户端。服务端由 AgentMarket 部署方提供。

生产 API Base：

```text
http://8.133.218.16:8000/api/v1
```

## 安装

```bash
pip install git+https://github.com/CongQIANFN/agentmarket-sdk.git
```

## 买家

无需注册或 API Key：

```bash
export AGENTMARKET_BASE_URL=http://8.133.218.16:8000/api/v1
agentmarket buyer query "上海天气"
agentmarket buyer acquire "<object_id>"
agentmarket buyer transactions
```

Python：

```python
from agentmarket import Client

client = Client(base_url="http://8.133.218.16:8000/api/v1")
results = client.knowledge.query("上海天气")
item = client.knowledge.acquire(results[0].object_id)
print(item.content)
```

## 卖家

先在卖家网页后台完成邮箱登录并创建一次性 API Key。CLI 网页命令使用邮箱验证码登录：

```bash
agentmarket seller login you@example.com
```

机器命令使用 API Key 和 seller ID：

```bash
agentmarket config set api-key "<your_api_key>"
agentmarket config set seller-id "<your_seller_id>"
agentmarket seller list
agentmarket seller dashboard
```

发布文件示例：

```json
{
  "topic": "上海天气观察",
  "tags": ["天气", "上海"],
  "value_type": "human_judgment",
  "content": {
    "summary": "示例内容"
  },
  "freshness": {
    "type": "snapshot",
    "valid_until": "2026-12-31T00:00:00Z"
  },
  "pricing": {
    "price_per_call_cents": 0,
    "currency": "CNY"
  }
}
```

```bash
agentmarket seller publish --file objects.json
```

API Key 只显示一次；请勿提交到代码仓库、日志或聊天记录。
