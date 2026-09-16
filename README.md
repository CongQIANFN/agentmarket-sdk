# AgentMarket SDK

AgentMarket 的公开 Python SDK 和 CLI 客户端。本仓库只包含客户端代码，不包含服务端、数据库、迁移或后台任务。

- Python 版本：**3.12+**
- 运行依赖：`httpx`
- 买家会话文件使用跨进程锁与原子写入，并发首次启动共用同一身份
- 默认 API Base：`http://localhost:8000/api/v1`
- 生产 API Base：`https://api.agentmarket.org.cn/api/v1`

## 安装

```bash
pip install git+https://github.com/CongQIANFN/agentmarket-sdk.git
```

也可以使用 `uv` 安装到当前环境：

```bash
uv tool install git+https://github.com/CongQIANFN/agentmarket-sdk.git
```

安装后会有两个入口：

```bash
python -c "import agentmarket; print(agentmarket.__version__)"
agentmarket --help
```

也可以直接查看版本号：

```bash
agentmarket --version
```

## 买家 / AI Agent 快速开始

无需注册或 API Key。

```python
from agentmarket import Client

client = Client(base_url="https://api.agentmarket.org.cn/api/v1")

results = client.knowledge.query("上海")
for item in results:
    print(item.object_id, item.topic, item.price_cents)

item = client.knowledge.acquire(results[0].object_id)
print(item.content)
```

CLI 示例：

```bash
export AGENTMARKET_BASE_URL=https://api.agentmarket.org.cn/api/v1

agentmarket buyer query "上海"
agentmarket buyer get <object_id>
agentmarket buyer acquire <object_id>
agentmarket buyer transactions
```

### 重复购买保护

付费对象在同一个 `base_url + session + object_id` 下成功交付后，默认再次 `acquire` 会被 SDK 拦截，错误码为 `91001`。免费对象不拦截；显式传入 `payment_proof` 时也不拦截。确认要再次购买时，显式使用：

```python
item = client.knowledge.acquire("<object_id>", repurchase=True)
```

```bash
agentmarket buyer acquire <object_id> --repurchase
```

SDK 会把成功交付记录写入 `$AGENTMARKET_HOME/purchases.jsonl`（默认 `~/.agentmarket/purchases.jsonl`），目录和账本权限分别是 `0700` 和 `0600`。账本只保存购买元数据，不保存 `Payment-Proof`。如果账本损坏，未传 proof 的付费 `acquire` 会 fail-closed 并返回明确的中文错误；免费 `acquire` 可以继续。SDK 不会静默清空、重建或截断账本。

付费对象会先返回 `402 Payment Required`。SDK 的 `PaymentRequiredError.bill` 是便于阅读和决策的**已解析账单**，不是可直接填入 `Payment-Proof` Header 的原始凭证。官方支付宝 402 买家支付会保存原始 `Payment-Needed` Header 与原始请求上下文，支付后自行恢复资源请求；自定义支付适配器取得平台认可的 proof 后，再调用 `knowledge.acquire(object_id, payment_proof=proof)`。

## 身份模式与切换

CLI 有两类身份，不要在同一个 `AGENTMARKET_HOME` 中混用：

| 模式 | 配置 | 适合命令 |
|---|---|---|
| 机器接入 | API Key + seller ID | `seller list`、`seller publish`、`seller dashboard` |
| 邮箱网页会话 | `seller login` 创建的 Cookie session | `seller me`、`seller applications`、`seller logout` |

同时存在 API Key 和 Cookie 会触发服务端/CLI 的 `90004` 身份冲突守卫。这是安全边界，不要绕过。

### 机器接入

```bash
export AGENTMARKET_BASE_URL=https://api.agentmarket.org.cn/api/v1

agentmarket config set api-key "<your_api_key>"
agentmarket config set seller-id "<your_seller_id>"

agentmarket seller list
agentmarket seller dashboard
```

这条路径不需要、也不要执行 `seller login` 或 `seller me`。

### 邮箱网页会话

```bash
export AGENTMARKET_BASE_URL=https://api.agentmarket.org.cn/api/v1
agentmarket seller login you@example.com
agentmarket seller me
agentmarket seller applications
```

这条路径不要再配置机器 API Key。

### 切换或并用

已有邮箱会话要切到机器接入时：

```bash
agentmarket seller logout
agentmarket config set api-key "<your_api_key>"
agentmarket config set seller-id "<your_seller_id>"
```

`seller logout` 只清除网页 session，不删除机器配置。需要长期并用时，为两个身份使用独立目录，并在每个命令前显式指定，避免环境变量串入另一个会话：

```bash
AGENTMARKET_HOME="$HOME/.agentmarket/machine" agentmarket seller list
AGENTMARKET_HOME="$HOME/.agentmarket/web" agentmarket seller login you@example.com
```

## 卖家常用命令

先在卖家网页后台完成邮箱登录并创建一次性 API Key；随后按上面的机器接入路径使用新配置目录配置 API Key 和 seller ID。不要先执行 `seller login` 再配置机器凭证。

```bash
export AGENTMARKET_BASE_URL=https://api.agentmarket.org.cn/api/v1

agentmarket config set api-key "<your_api_key>"
agentmarket config set seller-id "<your_seller_id>"

agentmarket seller list
agentmarket seller dashboard
```

发布托管数据：

```bash
cat > objects.json <<'JSON'
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
JSON

agentmarket seller publish --file objects.json
```

也可以直接用 Python：

```python
from agentmarket import Client

client = Client(
    base_url="https://api.agentmarket.org.cn/api/v1",
    api_key="<your_api_key>",
    seller_id="<your_seller_id>",
)

result = client.seller.publish_object({...})
print(result)
```

Route A 免费对象可以直接提交 `price_per_call_cents=0`。收费对象必须先读取档位并选择 `pricing_template_id`：

```python
options = client.seller.pricing_options()
option = next(item for item in options["items"] if item["price_cents"] == 1)
result = client.seller.publish_object(
    {...},
    pricing_template_id=option["template_id"],
)
```

网页会话请使用独立 `AGENTMARKET_HOME`；相关命令：

```bash
agentmarket seller applications
agentmarket seller logout
```

`logout` 只清理本地保存的网页 session，不会删除已配置的 API Key 或 seller ID。混用身份时，先 `seller logout`，再检查是否应切换到独立的机器配置目录。

## Admin 内容审核命令

管理员使用受权限保护的 API Key 和 seller ID 配置后审核对象：

```bash
export AGENTMARKET_BASE_URL=https://api.agentmarket.org.cn/api/v1
agentmarket config set api-key "<admin_api_key>"
agentmarket config set seller-id "<admin_seller_id>"

agentmarket admin review list --status pending_review
agentmarket admin review show <object_id>
agentmarket admin review content <object_id>
agentmarket admin review approve <object_id> --expected-version <content_version> --note "approved"
agentmarket admin review reject <object_id> --expected-version <content_version> --note "reason"
```

## 更多文档

- [人类用户接入指南](docs/onboarding-human.md)
- [AI Agent 接入指南](docs/onboarding-agent.md)

## 仓库边界

本仓库只同步必要的客户端实现：

- `agentmarket.sdk`
- `agentmarket.cli`

不包含：

- FastAPI 服务端；
- PostgreSQL 模型或迁移；
- Redis 后台任务；
- `.env*`、密钥、生产凭证或服务器配置。
