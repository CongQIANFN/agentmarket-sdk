import json

import httpx

import agentmarket
from agentmarket import cli
from agentmarket.update_check import check_update


def test_update_check_reports_up_to_date(monkeypatch, capsys):
    monkeypatch.setenv("AGENTMARKET_SDK_COMMIT", "a" * 40)
    monkeypatch.setattr("agentmarket.update_check._remote_main_commit", lambda: "a" * 40)
    result = check_update(agentmarket.__version__)
    assert result["status"] == "up_to_date"
    assert result["update_available"] is False

    assert cli.main(["update", "check"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "up_to_date"


def test_update_check_reports_update_available(monkeypatch, capsys):
    monkeypatch.setenv("AGENTMARKET_SDK_COMMIT", "a" * 40)
    monkeypatch.setattr("agentmarket.update_check._remote_main_commit", lambda: "b" * 40)
    result = check_update(agentmarket.__version__)
    assert result["status"] == "update_available"
    assert result["update_available"] is True


def test_update_check_handles_missing_local_commit(monkeypatch):
    monkeypatch.delenv("AGENTMARKET_SDK_COMMIT", raising=False)
    monkeypatch.setattr(
        "agentmarket.update_check._local_install_info",
        lambda: {"source": "unknown", "commit": None},
    )
    result = check_update(agentmarket.__version__)
    assert result["status"] == "unknown"
    assert result["remote_commit"] is None


def test_update_check_handles_network_error(monkeypatch):
    monkeypatch.setenv("AGENTMARKET_SDK_COMMIT", "a" * 40)

    def raise_http_error():
        raise httpx.ConnectError("offline")

    monkeypatch.setattr("agentmarket.update_check._remote_main_commit", raise_http_error)
    result = check_update(agentmarket.__version__)
    assert result["status"] == "unknown"
    assert result["update_available"] is False
    assert result["remote_commit"] is None


def test_update_check_disabled(monkeypatch):
    monkeypatch.setenv("AGENTMARKET_NO_UPDATE_CHECK", "1")
    result = check_update(agentmarket.__version__)
    assert result["status"] == "unknown"
    assert result["reason"] == "disabled by AGENTMARKET_NO_UPDATE_CHECK"
