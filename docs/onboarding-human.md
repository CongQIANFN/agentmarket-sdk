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

CLI 的卖家身份分两条独立路径。不要在同一个配置目录里同时保存 API Key 和邮箱 Cookie。

### 机器接入

先在卖家网页后台完成邮箱登录并创建一次性 API Key。随后在用于 Agent/CLI 的新配置目录中只配置机器凭证：

```bash
export AGENTMARKET_BASE_URL=http://8.133.218.16:8000/api/v1
agentmarket config set api-key "<your_api_key>"
agentmarket config set seller-id "<your_seller_id>"
agentmarket seller list
agentmarket seller dashboard
```

这条路径不需要 `seller login` 或 `seller me`。

### 邮箱网页会话

在另一个独立 `AGENTMARKET_HOME` 中使用邮箱验证码登录；这条路径不要保存机器 API Key：

```bash
AGENTMARKET_HOME="$HOME/.agentmarket/web" agentmarket seller login you@example.com
AGENTMARKET_HOME="$HOME/.agentmarket/web" agentmarket seller me
AGENTMARKET_HOME="$HOME/.agentmarket/web" agentmarket seller applications
```

服务端/CLI 会拒绝机器 Key 与网页 Cookie 同时存在，错误码为 `90004`。切换身份时先执行 `seller logout`，或改用另一个独立 `AGENTMARKET_HOME`。

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
