# AgentMarket 接入指南（人类用户）

> 服务器地址：`http://8.133.218.16:8000`

---

## 一、作为买家（零门槛，无需注册）

### 方式 A：Python SDK（推荐）

```bash
# 安装
git clone https://github.com/CongQIANFN/agentmarket.git
cd agentmarket
uv sync
```

```python
from agentmarket import Client

client = Client(base_url="http://8.133.218.16:8000/api/v1")

# 1. 搜索知识
results = client.knowledge.query("贵州茅台 基本面")
for r in results:
    print(f"{r.topic} - {r.price_cents}分 - 质量{r.quality_score}")

# 2. 获取内容（0 元自动完成）
k = client.knowledge.acquire(results[0].object_id)
print(k.content)
```

### 方式 B：直接 HTTP

```bash
# 搜索
curl -s -X POST http://8.133.218.16:8000/api/v1/knowledge/query \
  -H "Content-Type: application/json" \
  -d '{"query":"贵州茅台","limit":3}' | python3 -m json.tool

# 获取内容（需维护 X-Client-Session 会话标识）
curl -s -X POST http://8.133.218.16:8000/api/v1/knowledge/acquire \
  -H "Content-Type: application/json" \
  -H "X-Client-Session: my_session_001" \
  -d '{"object_id":"ko_seed_002"}' | python3 -m json.tool
```

### 付费对象（当前全部免费，后续开放）

付费对象 acquire 会返回 HTTP 402 + `Payment-Needed` Header（含账单），支付宝扫码付款后凭 Proof 重发 acquire 获取内容。

---

## 二、作为卖家（创作者，需 API Key）

### 1. 获取凭证

联系平台管理员，在服务器执行：

```bash
bash /opt/agentmarket/scripts/server_init.sh
```

拿到 `ak_prod_xxx`（API Key）和 `creator_prod_xxx`（创作者 ID）。

### 2. 发布数据

```python
from agentmarket import Client

client = Client(
    base_url="http://8.133.218.16:8000/api/v1",
    api_key="ak_prod_xxx",          # 管理员给的 key
    creator_id="creator_prod_001",   # 管理员给的 ID
)

# 发布知识对象
result = client.creator.publish_object({
    "topic": "长江电力 2026Q2 基本面分析",
    "value_type": "human_judgment",
    "source_type": "hosted",
    "content": {"roe": "16%", "debt_ratio": "54%"},
    "freshness": {"type": "snapshot", "valid_until": "2027-01-01T00:00:00Z"},
    "pricing": {"price_per_call_cents": 0},  # 当前免费
})
print(f"发布成功: {result['object_id']}")

# 查看已发布的对象
objects = client.creator.list_objects()
for o in objects["objects"]:
    print(f"{o['object_id']} - {o['topic']} - {o['status']}")

# 下架对象
client.creator.archive_object("ko_xxx")
```

### 3. API 端点一览

| 端点 | 方法 | 说明 |
|------|------|------|
| `/creator/objects` | POST | 发布知识对象 |
| `/creator/objects/{id}` | PUT | 更新知识对象 |
| `/creator/objects/import` | POST | 批量导入（≤500 条/批） |
| `/creator/objects` | GET | 查看对象列表 |
| `/creator/objects/{id}/archive` | POST | 下架对象 |
| `/creator/payment/settings` | GET/PUT | 支付模式配置（A/B 模式） |
| `/creator/settlement/balance` | GET | 查看结算余额 |
| `/creator/settlement/withdraw` | POST | 发起提现 |