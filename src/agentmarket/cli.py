"""AgentMarket 命令行客户端。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import httpx

from agentmarket.sdk import AgentMarketError, Client

DEFAULT_BASE_URL = "http://localhost:8000/api/v1"
SELLER_COOKIE_NAME = "am_seller_session"
CONFIG_FILE_NAME = "cli_config.json"
SELLER_MACHINE_COMMANDS = {"publish", "list", "dashboard"}


def home_dir() -> Path:
    return Path(os.environ.get("AGENTMARKET_HOME", Path.home() / ".agentmarket"))


def config_path() -> Path:
    return home_dir() / CONFIG_FILE_NAME


def _read_config() -> dict[str, Any]:
    try:
        value = json.loads(config_path().read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise AgentMarketError(90004, f"配置文件不是 JSON 对象: {config_path()}")
    return value


def _write_config(values: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(values, file, indent=2, ensure_ascii=False)
        file.write("\n")


def _effective_config() -> dict[str, Any]:
    stored = _read_config()
    return {
        "base_url": os.environ.get("AGENTMARKET_BASE_URL")
        or stored.get("base_url")
        or DEFAULT_BASE_URL,
        "api_key": os.environ.get("AGENTMARKET_API_KEY") or stored.get("api_key") or "",
        "seller_id": os.environ.get("AGENTMARKET_SELLER_ID") or stored.get("seller_id") or "",
        "seller_session": stored.get("seller_session") or "",
    }


def _masked(value: str) -> str:
    return "***" if value else ""


def _request_json(
    method: str,
    url: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    cookies: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[Any, httpx.Response]:
    try:
        response = httpx.request(
            method,
            url,
            json=json_body,
            params=params,
            cookies=cookies,
            headers=headers,
            timeout=30.0,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise AgentMarketError(90001, f"网络请求失败: {type(exc).__name__}: {exc}") from exc
    try:
        body = response.json()
    except ValueError:
        raise AgentMarketError(
            90000 + response.status_code, f"非 JSON 响应: HTTP {response.status_code}"
        ) from None
    if not (200 <= response.status_code < 300):
        code = body.get("code", response.status_code) if isinstance(body, dict) else None
        message = body.get("message", "unknown") if isinstance(body, dict) else "unknown"
        raise AgentMarketError(int(code or response.status_code), str(message))
    if not isinstance(body, dict) or body.get("code", 0) != 0:
        code = body.get("code", response.status_code) if isinstance(body, dict) else None
        message = body.get("message", "unknown") if isinstance(body, dict) else "unknown"
        raise AgentMarketError(int(code or response.status_code), str(message))
    return body.get("data", body), response


def _client_from_config() -> Client:
    config = _effective_config()
    return Client(
        base_url=config["base_url"],
        api_key=config["api_key"] or None,
        seller_id=config["seller_id"] or None,
    )


def _require_seller_session() -> str:
    session = _effective_config()["seller_session"]
    if not session:
        raise AgentMarketError(90003, "卖家未登录，请先执行 agentmarket seller login")
    return session


def _reject_mixed_seller_credentials(config: dict[str, Any]) -> None:
    if config["api_key"] and config["seller_session"]:
        raise AgentMarketError(
            90004,
            "卖家身份冲突：机器 API Key 与网页 Cookie session 不能同时使用；"
            "请只保留目标端点对应的身份凭证",
        )


def _seller_machine_request_options(config: dict[str, Any]) -> dict[str, Any]:
    """机器命令使用 API Key；完全未配置机器凭证时使用网页会话。"""
    api_key = config["api_key"]
    seller_id = config["seller_id"]
    if api_key or seller_id:
        _reject_mixed_seller_credentials(config)
        if not api_key or not seller_id:
            raise AgentMarketError(
                90004, "卖家机器命令需要同时配置 api-key 和 seller-id，或只使用 Cookie session"
            )
        return {"headers": {"Authorization": f"Bearer {api_key}", "X-Seller-Id": seller_id}}
    return {"cookies": {SELLER_COOKIE_NAME: _require_seller_session()}}


def _seller_web_request_options(config: dict[str, Any]) -> dict[str, Any]:
    """网页身份命令只接受 Cookie，API Key 不是该端点的替代身份。"""
    _reject_mixed_seller_credentials(config)
    return {"cookies": {SELLER_COOKIE_NAME: _require_seller_session()}}


def _seller_url(path: str) -> str:
    return f"{_effective_config()['base_url'].rstrip('/')}{path}"


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _load_publish_file(path: str) -> dict[str, Any] | list[dict[str, Any]]:
    try:
        value = json.loads(Path(path).read_text())
    except FileNotFoundError as exc:
        raise AgentMarketError(90004, f"文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AgentMarketError(90004, f"文件不是有效 JSON: {path}: {exc}") from exc
    if isinstance(value, dict) or (
        isinstance(value, list) and all(isinstance(item, dict) for item in value)
    ):
        return value
    raise AgentMarketError(90004, "发布文件必须是 JSON 对象或对象数组")


def _run_buyer(args: argparse.Namespace) -> int:
    if args.buyer_command == "rate":
        if not 1 <= args.rating <= 5:
            raise AgentMarketError(42201, "评分必须是 1-5 之间的整数")
        base_url = _effective_config()["base_url"].rstrip("/")
        client = _client_from_config()
        session = client.session.load_or_create()
        data, _ = _request_json(
            "POST",
            f"{base_url}/knowledge/{args.object_id}/rating",
            json_body={
                "transaction_id": args.transaction,
                "rating": args.rating,
                "used": args.used,
                "comment": args.comment,
            },
            headers={"X-Client-Session": session},
        )
        _print_json(data)
        return 0
    client = _client_from_config()
    if args.buyer_command == "query":
        _print_json([item.to_dict() for item in client.knowledge.query(args.query)])
    elif args.buyer_command == "get":
        _print_json(client.knowledge.get(args.object_id).to_dict())
    elif args.buyer_command == "acquire":
        _print_json(client.knowledge.acquire(args.object_id).to_dict())
    else:
        _print_json(client.transactions.list(page=args.page))
    return 0


def _run_seller_login(args: argparse.Namespace) -> int:
    base_url = _effective_config()["base_url"].rstrip("/")
    challenge, _ = _request_json(
        "POST",
        f"{base_url}/seller-auth/email-code",
        json_body={"email": args.email},
    )
    if not isinstance(challenge, dict):
        raise AgentMarketError(90004, "邮箱验证码接口返回格式异常")
    mock_code = challenge.get("mock_code")
    if mock_code:
        print(f"开发模式验证码: {mock_code}")
    code = input("请输入验证码: ")
    payload = {
        "email": args.email,
        "challenge_id": challenge.get("challenge_id"),
        "code": code,
    }
    data, response = _request_json(
        "POST",
        f"{base_url}/seller-auth/email-session",
        json_body=payload,
    )
    raw_cookies = "\n".join(response.headers.get_list("set-cookie"))
    cookies = SimpleCookie()
    cookies.load(raw_cookies)
    morsel = cookies.get(SELLER_COOKIE_NAME)
    session_value = morsel.value if morsel else ""
    if not session_value:
        raise AgentMarketError(90004, "登录响应缺少 am_seller_session cookie")
    config = _read_config()
    config["seller_session"] = session_value
    _write_config(config)
    _print_json(data)
    return 0


def _run_seller(args: argparse.Namespace) -> int:
    if args.seller_command == "login":
        return _run_seller_login(args)
    config = _effective_config()
    if args.seller_command in SELLER_MACHINE_COMMANDS:
        request_options = _seller_machine_request_options(config)
        if args.seller_command == "publish":
            payload = _load_publish_file(args.file)
            is_import = isinstance(payload, list) or "objects" in payload
            json_body = {"objects": payload} if isinstance(payload, list) else payload
            data, _ = _request_json(
                "POST",
                _seller_url("/seller/objects/import" if is_import else "/seller/objects"),
                json_body=json_body,
                **request_options,
            )
        elif args.seller_command == "list":
            data, _ = _request_json(
                "GET",
                _seller_url("/seller/objects"),
                params={"page": args.page, **({"status": args.status} if args.status else {})},
                **request_options,
            )
        else:
            data, _ = _request_json(
                "GET",
                _seller_url("/seller/dashboard"),
                params={"period": args.period},
                **request_options,
            )
    else:
        if args.seller_command == "logout":
            # logout 是清理网页 session 的动作；保留机器凭证，不应被混合凭证守卫阻止。
            request_options = {"cookies": {SELLER_COOKIE_NAME: _require_seller_session()}}
        else:
            request_options = _seller_web_request_options(config)
        cookies = request_options["cookies"]
        if args.seller_command == "me":
            data, _ = _request_json("GET", _seller_url("/seller/me"), cookies=cookies)
        elif args.seller_command == "link-seller":
            data, _ = _request_json(
                "POST",
                _seller_url("/seller/link-seller"),
                json_body={"seller_api_key": args.api_key},
                cookies=cookies,
            )
        elif args.seller_command == "apply":
            data, _ = _request_json(
                "POST",
                _seller_url("/seller/applications"),
                json_body={
                    "display_name": args.display_name,
                    "subject_type": args.subject_type,
                    "intended_route": args.route,
                    "content_summary": args.summary,
                    "planned_pricing_mode": args.pricing,
                },
                cookies=cookies,
            )
        elif args.seller_command == "applications":
            data, _ = _request_json("GET", _seller_url("/seller/applications"), cookies=cookies)
        elif args.seller_command == "application":
            data, _ = _request_json(
                "GET", _seller_url(f"/seller/applications/{args.application_id}"), cookies=cookies
            )
        else:
            data, _ = _request_json(
                "POST", _seller_url("/seller-auth/logout"), json_body={}, cookies=cookies
            )
            stored = _read_config()
            stored["seller_session"] = ""
            _write_config(stored)
    _print_json(data)
    return 0


def _run_admin(args: argparse.Namespace) -> int:
    config = _effective_config()
    base_url = config["base_url"].rstrip("/")
    api_key = config["api_key"]
    seller_id = config["seller_id"]
    if not api_key or not seller_id:
        raise AgentMarketError(90004, "请先配置 admin 使用的 api-key 和 seller-id")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Seller-Id": seller_id,
    }
    if args.admin_command == "review":
        if args.review_command == "list":
            params = {
                "page": args.page,
                "status": args.status,
                "route_type": args.route_type,
                "seller_id": args.seller_id,
            }
            data, _ = _request_json(
                "GET",
                f"{base_url}/admin/review-objects",
                params={k: v for k, v in params.items() if v is not None},
                headers=headers,
            )
        elif args.review_command == "show":
            data, _ = _request_json(
                "GET",
                f"{base_url}/admin/review-objects/{args.object_id}",
                headers=headers,
            )
        elif args.review_command == "content":
            data, _ = _request_json(
                "GET",
                f"{base_url}/admin/review-objects/{args.object_id}/content",
                headers=headers,
            )
        elif args.review_command == "approve":
            payload = {"expected_version": args.expected_version}
            if args.note is not None:
                payload["note"] = args.note
            data, _ = _request_json(
                "POST",
                f"{base_url}/admin/review-objects/{args.object_id}/approve",
                json_body=payload,
                headers=headers,
            )
        else:
            data, _ = _request_json(
                "POST",
                f"{base_url}/admin/review-objects/{args.object_id}/reject",
                json_body={"note": args.note, "expected_version": args.expected_version},
                headers=headers,
            )
        _print_json(data)
        return 0
    raise AgentMarketError(90004, "不支持的 admin 子命令")


def _run_config(args: argparse.Namespace) -> int:
    if args.config_command == "show":
        config = _effective_config()
        config["api_key"] = _masked(config["api_key"])
        config["seller_session"] = _masked(config["seller_session"])
        _print_json(config)
        return 0
    config = _read_config()
    if args.key == "base-url":
        config["base_url"] = args.value
    elif args.key == "api-key":
        config["api_key"] = args.value
    else:
        config["seller_id"] = args.value
    _write_config(config)
    _print_json({"saved": True, "key": args.key})
    return 0


def build_parser() -> argparse.ArgumentParser:
    def positive_int(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("必须是正整数") from exc
        if parsed < 1:
            raise argparse.ArgumentTypeError("必须是正整数")
        return parsed

    parser = argparse.ArgumentParser(prog="agentmarket")
    commands = parser.add_subparsers(dest="command", required=True)

    buyer = commands.add_parser("buyer", help="买方命令")
    buyer_commands = buyer.add_subparsers(dest="buyer_command", required=True)
    buyer_query = buyer_commands.add_parser("query")
    buyer_query.add_argument("query")
    buyer_get = buyer_commands.add_parser("get")
    buyer_get.add_argument("object_id")
    buyer_acquire = buyer_commands.add_parser("acquire")
    buyer_acquire.add_argument("object_id")
    buyer_transactions = buyer_commands.add_parser("transactions")
    buyer_transactions.add_argument("--page", type=int, default=1)
    buyer_rate = buyer_commands.add_parser("rate")
    buyer_rate.add_argument("object_id")
    buyer_rate.add_argument("--transaction", required=True)
    buyer_rate.add_argument("--rating", type=int, required=True)
    buyer_rate.add_argument("--comment", default="")
    buyer_rate.add_argument("--used", action="store_true")
    buyer.set_defaults(func=_run_buyer)

    seller = commands.add_parser("seller", help="卖家命令")
    seller_commands = seller.add_subparsers(dest="seller_command", required=True)
    seller_login = seller_commands.add_parser("login")
    seller_login.add_argument("email")
    seller_commands.add_parser("me")
    seller_link = seller_commands.add_parser("link-seller")
    seller_link.add_argument("api_key")
    seller_apply = seller_commands.add_parser("apply")
    seller_apply.add_argument("--display-name", required=True)
    seller_apply.add_argument("--subject-type", required=True)
    seller_apply.add_argument("--route", required=True)
    seller_apply.add_argument("--summary", required=True)
    seller_apply.add_argument("--pricing", required=True)
    seller_commands.add_parser("applications")
    seller_application = seller_commands.add_parser("application")
    seller_application.add_argument("application_id")
    seller_commands.add_parser("logout")
    seller.set_defaults(func=_run_seller)

    seller_publish = seller_commands.add_parser("publish")
    seller_publish.add_argument("--file", required=True)
    seller_list = seller_commands.add_parser("list")
    seller_list.add_argument("--page", type=int, default=1)
    seller_list.add_argument("--status")
    seller_dashboard = seller_commands.add_parser("dashboard")
    seller_dashboard.add_argument("--period", default="30d")

    admin = commands.add_parser("admin", help="Admin 管理命令")
    admin_commands = admin.add_subparsers(dest="admin_command", required=True)
    review = admin_commands.add_parser("review")
    review_commands = review.add_subparsers(dest="review_command", required=True)
    review_list = review_commands.add_parser("list")
    review_list.add_argument("--page", type=int, default=1)
    review_list.add_argument("--status")
    review_list.add_argument("--route-type")
    review_list.add_argument("--seller-id")
    review_show = review_commands.add_parser("show")
    review_show.add_argument("object_id")
    review_content = review_commands.add_parser("content")
    review_content.add_argument("object_id")
    review_approve = review_commands.add_parser("approve")
    review_approve.add_argument("object_id")
    review_approve.add_argument("--expected-version", required=True, type=positive_int)
    review_approve.add_argument("--note")
    review_reject = review_commands.add_parser("reject")
    review_reject.add_argument("object_id")
    review_reject.add_argument("--expected-version", required=True, type=positive_int)
    review_reject.add_argument("--note", required=True)
    admin.set_defaults(func=_run_admin)

    config = commands.add_parser("config", help="CLI 配置")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("show")
    config_set = config_commands.add_parser("set")
    config_set.add_argument("key", choices=["base-url", "api-key", "seller-id"])
    config_set.add_argument("value")
    config.set_defaults(func=_run_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except AgentMarketError as exc:
        print(f"错误 [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
