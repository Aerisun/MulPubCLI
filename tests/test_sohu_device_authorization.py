"""Password identity and device authorization must both survive credential reuse."""

import json
from unittest.mock import Mock, patch

import pytest

from mulpubcli.http import HTTPFailure
from mulpubcli.platforms.sohu.client import ACC_API, LIST_API, SohuWeb
from mulpubcli.platforms.sohu.login import SohuLogin


def response(payload):
    result = Mock(status_code=200)
    result.json.return_value = payload
    return result


def account_response():
    return response(
        {
            "code": 2000000,
            "data": {"data": [{"accounts": [{"id": 123, "nickName": "Synthetic"}]}]},
        }
    )


@pytest.mark.parametrize(
    "code,kind", [(1300, "verification_required"), (1211, "authentication_required")]
)
def test_password_identity_does_not_bypass_device_check(code, kind):
    client = SohuWeb(account_id="123", dv_id="device", sp_cm="device-authorization")
    client.http.request = Mock(
        side_effect=[
            account_response(),
            response(
                {"code": code, "msg": "opaque-phone-and-platform-data", "data": []}
            ),
        ]
    )
    try:
        with pytest.raises(HTTPFailure) as failure:
            client.account()
        assert failure.value.kind == kind
        assert failure.value.code == code
        assert "opaque-phone" not in failure.value.user_message()
        assert failure.value.safe_details()["request_target"] == LIST_API.removeprefix(
            "https://"
        )
    finally:
        client.close()


def test_device_check_is_a_single_small_read_before_accepting_identity():
    client = SohuWeb(account_id="123", dv_id="device", sp_cm="authorized")
    client.http.request = Mock(
        side_effect=[
            account_response(),
            response({"code": 2000000, "data": {"news": [], "totalCount": 0}}),
        ]
    )
    try:
        assert client.account()["id"] == "123"
        calls = client.http.request.call_args_list
        assert len(calls) == 2
        assert calls[0].args[1].startswith(ACC_API)
        assert calls[1].args == ("GET", LIST_API)
        assert calls[1].kwargs["params"]["psize"] == 1
        assert calls[1].kwargs["params"]["accountId"] == "123"
        assert calls[1].kwargs["headers"]["sp-cm"] == "authorized"
    finally:
        client.close()


@pytest.mark.parametrize(
    "payload", [[], {"code": 2000000, "data": []}, {"code": 2000000, "data": {}}]
)
def test_device_check_does_not_accept_malformed_success(payload):
    client = SohuWeb(account_id="123")
    client.http.request = Mock(side_effect=[account_response(), response(payload)])
    try:
        with pytest.raises(HTTPFailure) as failure:
            client.account()
        assert failure.value.kind == "invalid_response"
    finally:
        client.close()


def test_export_retains_the_device_headers_used_by_the_current_page(tmp_path):
    target = tmp_path / "sohu.json"
    login = SohuLogin("synthetic", "synthetic", target, account_id="123")
    page = Mock()
    page.context.cookies.return_value = [
        {
            "name": "mp-cv",
            "value": "stale-cookie",
            "domain": ".sohu.com",
            "path": "/",
            "secure": True,
            "expires": 2_000_000_000,
        }
    ]
    page.evaluate.side_effect = lambda expression: (
        "authorized-current-device" if "preview-sp-cm" in expression else "current-dv"
    )
    login._export(page)
    stored = json.loads(target.read_text())
    assert stored["sp_cm"] == "authorized-current-device"
    assert stored["dv_id"] == "current-dv"
    assert stored["cookies"][0]["expires"] == 2_000_000_000
    client = SohuWeb.load(target)
    try:
        assert client._write_headers()["sp-cm"] == "authorized-current-device"
        assert client._write_headers()["dv-id"] == "current-dv"
    finally:
        client.close()


def test_device_probe_waits_for_late_sms_route_before_browser_closes(tmp_path):
    login = SohuLogin("synthetic", "synthetic", tmp_path / "sohu.json")
    page = Mock()
    page.context.cookies.return_value = [
        {"name": "mp-cv", "value": "old", "domain": "mp.sohu.com"}
    ]
    page.url = "https://mp.sohu.com/mpfe/v4/contentManagement/first/page"
    page.evaluate.return_value = "作品列表"
    client = Mock()
    client.account.side_effect = [
        HTTPFailure("设备未授权", kind="verification_required", code=1300),
        {"id": "123", "name": "Synthetic"},
    ]

    def delayed_route(expression, **kwargs):
        if "clientAuth" in expression and "contenteditable" not in expression:
            page.url = "https://mp.sohu.com/mpfe/v4/clientAuth"

    page.wait_for_function.side_effect = delayed_route

    def solve_sms(current):
        assert current.url.endswith("/clientAuth")
        current.url = "https://mp.sohu.com/mpfe/v4/contentManagement/first/page"
        return True

    with (
        patch.object(login, "_export"),
        patch.object(login, "_solve_sms_code", side_effect=solve_sms) as sms,
        patch.object(SohuWeb, "load", return_value=client),
    ):
        assert login._perform(page, None, None) is False
    sms.assert_called_once_with(page)
    assert client.account.call_count == 2
    assert client.close.call_count == 2


def test_unattended_device_rejection_preserves_credential_without_sending_sms(tmp_path):
    target = tmp_path / "sohu.json"
    target.write_text("original")
    login = SohuLogin("synthetic", "synthetic", target, allow_human=False)
    page = Mock()
    page.context.cookies.return_value = [
        {"name": "mp-cv", "value": "old", "domain": "mp.sohu.com"}
    ]
    page.url = "https://mp.sohu.com/mpfe/v4/contentManagement/first/page"
    page.evaluate.return_value = "作品列表"
    client = Mock()
    client.account.side_effect = HTTPFailure(
        "设备未授权", kind="verification_required", code=1300
    )

    def browser_login(_url, perform, **kwargs):
        perform(page, None, None)
        raise AssertionError("unverified browser must not finish")

    with (
        patch("mulpubcli.browser.PlaywrightLoginer") as browser,
        patch.object(SohuWeb, "load", return_value=client),
        patch.object(login, "_solve_sms_code") as sms,
    ):
        browser.return_value.login.side_effect = browser_login
        with pytest.raises(HTTPFailure) as failure:
            login.run()
    assert failure.value.kind == "verification_required"
    sms.assert_not_called()
    assert target.read_text() == "original"
    assert list(tmp_path.glob(".sohu-login-*")) == []
