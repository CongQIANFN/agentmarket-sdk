from __future__ import annotations

import base64
import json
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import httpx
import pytest

from agentmarket import cli
from agentmarket.purchase_history import PurchaseHistory
from agentmarket.sdk import AgentMarketError, Client


def _body(data=None, *, code=0):
    return {"code": code, "message": "ok", "data": data}


def _payment_needed(amount: str = "0.01", out_trade_no: str = "trade-1") -> str:
    bill = {
        "protocol": {
            "resource_id": "object-1",
            "amount": amount,
            "out_trade_no": out_trade_no,
        }
    }
    return base64.urlsafe_b64encode(json.dumps(bill).encode()).decode()


def _client(tmp_path: Path, handler, *, auto_pay: bool = True) -> Client:
    os.environ["AGENTMARKET_HOME"] = str(tmp_path)
    return Client(
        base_url="http://testserver/api/v1",
        auto_pay=auto_pay,
        _transport=httpx.MockTransport(handler),
    )


def _route(price_cents: int = 10) -> dict:
    return {
        "object_id": "object-1",
        "topic": "季度财报",
        "route_type": "platform_hosted",
        "price_cents": price_cents,
    }


def _paid_delivery() -> dict:
    return {
        "object_id": "object-1",
        "content": {"summary": "ok"},
        "metadata": {},
        "transaction_id": "transaction-1",
        "content_version": 7,
    }


def _route_a_handler(route: dict, delivery: dict, calls: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "GET" and request.url.path.endswith("/knowledge/object-1"):
            return httpx.Response(200, json=_body(route))
        if request.method == "POST" and request.url.path.endswith("/knowledge/acquire"):
            if not request.headers.get("Payment-Proof"):
                return httpx.Response(
                    402,
                    json=_body({"message": "Payment Required"}, code=40200),
                    headers={"Payment-Needed": _payment_needed()},
                )
            return httpx.Response(200, json=_body(delivery))
        if request.method == "POST" and request.url.path.endswith("/payment/confirm"):
            return httpx.Response(200, json=_body({"proof": "free-proof"}))
        if request.method == "POST" and request.url.path.endswith("/payment/mock-pay"):
            return httpx.Response(200, json=_body({"proof": "mock-proof"}))
        return httpx.Response(404, json=_body(None, code=40400))

    return handler


def _append_worker(path: str, index: int) -> None:
    history = PurchaseHistory(Path(path), base_url="http://testserver/api/v1", session="cs_same")
    history.record(
        object_id=f"object-{index}",
        topic="topic",
        route_type="platform_hosted",
        amount_cents=1,
        transaction_id=f"transaction-{index}",
        out_trade_no=f"trade-{index}",
        content_version=1,
        sdk_version="test",
    )


def test_base_url_rejects_blank_invalid_and_missing_host(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTMARKET_HOME", str(tmp_path))
    for value in ["", "   ", "ftp://example.com", "http://"]:
        code = cli.main(["config", "set", "base-url", value])
        assert code == 1
        assert "错误 [90004]" in capsys.readouterr().err

    assert cli.main(["config", "set", "base-url", "http://localhost:8000/api/v1"]) == 0
    assert cli.main(["config", "set", "base-url", "https://api.example.com/api/v1"]) == 0


def test_config_allows_explicit_clearing(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTMARKET_HOME", str(tmp_path))
    assert cli.main(["config", "set", "api-key", "key"]) == 0
    assert cli.main(["config", "set", "api-key", ""]) == 0
    assert cli.main(["config", "set", "seller-id", "seller"]) == 0
    assert cli.main(["config", "set", "seller-id", ""]) == 0


def test_global_version(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTMARKET_HOME", str(tmp_path))
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out == "0.2.4\n"


def test_cli_rate_returns_confirmation_without_comment(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTMARKET_HOME", str(tmp_path))
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs["json_body"]))
        return {"rated": True}, httpx.Response(200)

    monkeypatch.setattr(cli, "_request_json", fake_request)
    assert (
        cli.main(
            [
                "buyer",
                "rate",
                "object-1",
                "--transaction",
                "transaction-1",
                "--rating",
                "5",
                "--comment",
                "secret-comment",
                "--used",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "rated": True,
        "object_id": "object-1",
        "transaction_id": "transaction-1",
        "rating": 5,
        "used": True,
    }
    assert "secret-comment" not in output


def test_paid_repeated_acquire_is_blocked(tmp_path):
    calls = []
    client = _client(tmp_path, _route_a_handler(_route(), _paid_delivery(), calls))
    client.knowledge.acquire("object-1")
    client.close()

    repeat_client = _client(tmp_path, _route_a_handler(_route(), _paid_delivery(), calls))
    with pytest.raises(AgentMarketError) as excinfo:
        repeat_client.knowledge.acquire("object-1")
    assert excinfo.value.code == 91001
    assert "repurchase=True" in excinfo.value.message
    assert "--repurchase" in excinfo.value.message
    repeat_client.close()


def test_repurchase_explicitly_allows_paid_acquire(tmp_path):
    calls = []
    client = _client(tmp_path, _route_a_handler(_route(), _paid_delivery(), calls))
    client.knowledge.acquire("object-1")
    acquired = client.knowledge.acquire("object-1", repurchase=True)
    assert acquired.object_id == "object-1"
    client.close()


def test_free_object_is_not_blocked(tmp_path):
    calls = []
    client = _client(tmp_path, _route_a_handler(_route(0), _paid_delivery(), calls))
    client.knowledge.acquire("object-1")
    acquired = client.knowledge.acquire("object-1")
    assert acquired.object_id == "object-1"
    client.close()


def test_explicit_payment_proof_is_not_blocked(tmp_path):
    history = PurchaseHistory(tmp_path, base_url="http://testserver/api/v1", session=None)
    history.append({"not": "a valid ledger entry"})
    calls = []
    os.environ["AGENTMARKET_HOME"] = str(tmp_path)
    client = Client(
        base_url="http://testserver/api/v1",
        _transport=httpx.MockTransport(_route_a_handler(_route(), _paid_delivery(), calls)),
    )
    acquired = client.knowledge.acquire("object-1", payment_proof="real-proof")
    assert acquired.content == {"summary": "ok"}
    assert calls[1].endswith("/knowledge/acquire")
    client.close()


def test_matching_key_does_not_confuse_endpoints_sessions_or_objects(tmp_path):
    base = "http://testserver/api/v1"
    history = PurchaseHistory(tmp_path, base_url=base, session="cs-a")
    history.record(
        object_id="object-1",
        topic="topic",
        route_type="platform_hosted",
        amount_cents=10,
        transaction_id="transaction-1",
        out_trade_no="trade-1",
        content_version=1,
        sdk_version="test",
    )
    variants = [
        PurchaseHistory(tmp_path, base_url="http://other/api/v1", session="cs-a"),
        PurchaseHistory(tmp_path, base_url=base, session="cs-b"),
    ]
    for variant in variants:
        assert variant.find_delivered("object-1") is None
    assert history.find_delivered("object-2") is None
    assert history.find_delivered("object-1") is not None


def test_corrupt_ledger_fails_closed_for_paid_acquire(tmp_path):
    ledger = tmp_path / "purchases.jsonl"
    ledger.write_text("{broken\n", encoding="utf-8")
    calls = []
    client = _client(tmp_path, _route_a_handler(_route(), _paid_delivery(), calls))
    with pytest.raises(AgentMarketError) as excinfo:
        client.knowledge.acquire("object-1")
    assert excinfo.value.code == 91002
    assert "第 1 行损坏" in excinfo.value.message
    client.close()


def test_successful_acquire_records_complete_fields_without_proof(tmp_path):
    calls = []
    client = _client(tmp_path, _route_a_handler(_route(), _paid_delivery(), calls))
    acquired = client.knowledge.acquire("object-1")
    assert acquired.to_dict() == {
        "object_id": "object-1",
        "content": {"summary": "ok"},
        "seller": {},
        "freshness": {},
        "degraded": False,
        "disclaimer": "",
        "metadata": {},
        "content_version": 7,
        "content_created_at": None,
        "metadata_status": "unavailable",
    }
    record = json.loads((tmp_path / "purchases.jsonl").read_text(encoding="utf-8"))
    assert record == {
        "ledger_schema_version": 1,
        "sdk_version": "0.2.4",
        "ts": record["ts"],
        "base_url": "http://testserver/api/v1",
        "session": record["session"],
        "object_id": "object-1",
        "topic": "季度财报",
        "route_type": "platform_hosted",
        "outcome": "delivered",
        "amount_cents": 1,
        "transaction_id": "transaction-1",
        "out_trade_no": "trade-1",
        "content_version": 7,
    }
    assert "proof" not in record
    assert "mock-proof" not in (tmp_path / "purchases.jsonl").read_text(encoding="utf-8")
    assert oct(tmp_path.stat().st_mode & 0o777) == "0o700"
    assert oct((tmp_path / "purchases.jsonl").stat().st_mode & 0o777) == "0o600"
    client.close()


def test_concurrent_appends_do_not_tear_lines(tmp_path):
    with ProcessPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_append_worker, str(tmp_path), index) for index in range(40)]
        for future in futures:
            future.result()
    lines = (tmp_path / "purchases.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 40
    assert all(isinstance(json.loads(line), dict) for line in lines)


def test_cli_acquire_output_does_not_leak_proof(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTMARKET_HOME", str(tmp_path))

    class FakeDelivery:
        def __init__(self, data):
            self._data = data

        def to_dict(self):
            return {
                "object_id": self._data["object_id"],
                "repurchase": self._data["repurchase"],
            }

    class FakeKnowledge:
        def acquire(self, object_id, repurchase=False):
            return FakeDelivery(
                {
                    "object_id": object_id,
                    "repurchase": repurchase,
                    "proof": "secret-proof",
                }
            )

    class FakeClient:
        knowledge = FakeKnowledge()

    monkeypatch.setattr(cli, "_client_from_config", lambda: FakeClient())
    assert cli.main(["buyer", "acquire", "object-1"]) == 0
    output = capsys.readouterr().out
    assert "secret-proof" not in output
    assert "proof" not in output
