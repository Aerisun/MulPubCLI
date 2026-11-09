"""Login and NetEase submission failures expose fixed reasons, never payloads."""

import json
from unittest.mock import Mock, patch

import pytest

from mulpubcli.browser import LoginResult
from mulpubcli.core import Article
from mulpubcli.http import HTTPFailure
from mulpubcli.platforms.netease.client import NeteaseWeb
from mulpubcli.platforms.netease.login import NeteaseLogin
from mulpubcli.platforms.sohu.login import SohuLogin


@pytest.mark.parametrize(
    "platform,login_type", [("netease", NeteaseLogin), ("sohu", SohuLogin)]
)
@pytest.mark.parametrize("stage", ["load", "account"])
@pytest.mark.parametrize(
    "kind,code", [("authentication_required", 1211), ("verification_required", 1300)]
)
def test_login_verification_failure_returns_safe_reason_and_keeps_old_credential(
    tmp_path,
    platform,
    login_type,
    stage,
    kind,
    code,
):
    target = tmp_path / f"{platform}.json"
    target.write_text("original-credential", encoding="utf-8")
    login = login_type("synthetic-phone", "synthetic-password", target)
    failure = HTTPFailure(
        "password=secret raw response body",
        kind=kind,
        code=code,
        status_code=200,
        request_method="GET",
        request_target=f"https://mp.{platform}.com/api/account?token=secret",
    )

    def browser_login(*_args, **_kwargs):
        login.destination.write_text("unverified-credential", encoding="utf-8")
        return LoginResult(status="ok")

    client = Mock()
    client.account.side_effect = failure
    client_name = "NeteaseWeb" if platform == "netease" else "SohuWeb"
    with (
        patch("mulpubcli.browser.PlaywrightLoginer") as browser,
        patch(f"mulpubcli.platforms.{platform}.client.{client_name}.load") as load,
    ):
        browser.return_value.login.side_effect = browser_login
        if stage == "load":
            load.side_effect = failure
        else:
            load.return_value = client
        result = login.run()

    assert result["status"] == "failed"
    assert failure.user_message() in result["message"]
    assert all(
        result.get(key) == value for key, value in failure.safe_details().items()
    )
    assert "secret" not in json.dumps(result)
    assert "raw response body" not in json.dumps(result)
    assert target.read_text(encoding="utf-8") == "original-credential"
    assert login.destination == target
    assert list(tmp_path.glob(f".{platform}-login-*")) == []
    if stage == "account":
        client.close.assert_called_once()


@pytest.mark.parametrize(
    "operation,kind",
    [
        ("draft", "network_timeout"),
        ("publish", "verification_required"),
        ("publish", "limit"),
    ],
)
def test_netease_submission_failure_preserves_status_without_raw_payload(
    tmp_path,
    operation,
    kind,
):
    client = NeteaseWeb.__new__(NeteaseWeb)
    failure = HTTPFailure("cookie=secret raw response body", kind=kind, code=5001)
    client._submit = Mock(side_effect=failure)
    article = Article(
        title="Synthetic title", body="Synthetic body", cover=tmp_path / "cover.png"
    )
    with patch(
        "mulpubcli.platforms.netease.client.netease_guard.load_urs_token",
        return_value="synthetic-token",
    ):
        result = getattr(client, operation)(article)

    assert result.status == ("failed" if kind == "limit" else "pending")
    assert failure.user_message() in result.message
    assert "secret" not in result.message and "raw response body" not in result.message
    assert result.url is None


def test_netease_token_preflight_failure_is_safe_and_never_submits(tmp_path):
    client = NeteaseWeb.__new__(NeteaseWeb)
    client._submit = Mock()
    failure = HTTPFailure(
        "token=secret raw response body", kind="authentication_required"
    )
    article = Article(
        title="Synthetic title", body="Synthetic body", cover=tmp_path / "cover.png"
    )
    with patch(
        "mulpubcli.platforms.netease.client.netease_guard.load_urs_token",
        side_effect=failure,
    ):
        result = client.publish(article)

    assert result.status == "failed"
    assert failure.user_message() in result.message
    assert "secret" not in result.message and "raw response body" not in result.message
    client._submit.assert_not_called()
