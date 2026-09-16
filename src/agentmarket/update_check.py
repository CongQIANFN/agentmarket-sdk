"""CLI 版本检查。

main 分支按“当前测试发布分支”管理；这里不依赖 git tag，而是比较本地 git 安装
来源和远端 main 的 commit。网络或本地元数据不可用时返回 unknown，不阻塞业务。
"""

from __future__ import annotations

import json
import os
from importlib.metadata import distribution
from typing import Any
from urllib.parse import urlparse

import httpx

SDK_REPOSITORY = "CongQIANFN/agentmarket-sdk"
INSTALL_COMMAND = (
    "pip install --force-reinstall git+https://github.com/CongQIANFN/agentmarket-sdk.git"
)


def _local_install_info() -> dict[str, Any]:
    override = os.environ.get("AGENTMARKET_SDK_COMMIT", "").strip()
    if override:
        return {"source": "environment", "commit": override}

    dist = distribution("agentmarket-sdk")
    raw = dist.read_text("direct_url.json")
    if not raw:
        return {"source": "unknown", "commit": None}
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"source": "unknown", "commit": None}
    url = data.get("url") if isinstance(data, dict) else None
    source = "unknown"
    if isinstance(url, str):
        parsed = urlparse(url)
        if parsed.scheme == "https" and parsed.hostname == "github.com":
            source = "github"
        elif parsed.scheme in ("http", "https"):
            source = "remote"
        elif parsed.scheme in ("file", ""):
            source = "local"
    commit = None
    vcs_info = data.get("vcs_info")
    if isinstance(vcs_info, dict):
        value = vcs_info.get("commit")
        if isinstance(value, str) and value:
            commit = value
    return {"source": source, "commit": commit}


def _remote_main_commit() -> str:
    url = f"https://api.github.com/repos/{SDK_REPOSITORY}/commits/main"
    response = httpx.get(
        url,
        timeout=3.0,
        follow_redirects=False,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "agentmarket-sdk-update-check",
        },
    )
    if response.status_code != 200:
        raise RuntimeError(f"GitHub API returned HTTP {response.status_code}")
    payload = response.json()
    commit = payload.get("sha") if isinstance(payload, dict) else None
    if not isinstance(commit, str) or not commit:
        raise RuntimeError("GitHub API response missing commit SHA")
    return commit


def check_update(current_version: str) -> dict[str, Any]:
    """返回可 JSON 序列化的检查结果；网络/元数据异常不影响调用方。"""
    from agentmarket import __version__

    result: dict[str, Any] = {
        "channel": "main",
        "current_version": current_version or __version__,
        "current_commit": None,
        "remote_commit": None,
        "source": None,
        "update_available": False,
        "status": "unknown",
        "install_command": INSTALL_COMMAND,
    }
    if os.environ.get("AGENTMARKET_NO_UPDATE_CHECK", "").strip() in {"1", "true", "yes"}:
        result["reason"] = "disabled by AGENTMARKET_NO_UPDATE_CHECK"
        return result

    local = _local_install_info()
    result["source"] = local["source"]
    result["current_commit"] = local["commit"]
    if not local["commit"]:
        result["reason"] = "本地安装元数据缺少 commit；请用 git+https URL 重新安装"
        return result

    try:
        remote_commit = _remote_main_commit()
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        result["reason"] = f"远端检查失败: {exc}"
        return result

    result["remote_commit"] = remote_commit
    if local["commit"] == remote_commit:
        result["status"] = "up_to_date"
        return result

    result["status"] = "update_available"
    result["update_available"] = True
    return result
