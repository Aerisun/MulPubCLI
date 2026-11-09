"""Publishing errors preserve submission uncertainty without exposing credentials."""

import json
from types import SimpleNamespace

import pytest
import requests

from mulpubcli import __main__ as cli
from mulpubcli.core import Article
from mulpubcli.http import HTTP, HTTPFailure
from mulpubcli.platforms.sohu.client import ACC_API, SohuWeb
from mulpubcli.storage import StorageLayout


def test_failure_safe_details_are_a_whitelist_and_preserve_legacy_output():
    failure = HTTPFailure(
        "password=secret raw response body",
        kind="verification_required",
        status_code=403,
        code=40352,
        request_method="post",
        request_target="https://person:secret@mp.sohu.com/api/submit?token=secret#secret",
        redirect_target="https://mp.sohu.com/login?password=secret",
    )
    assert failure.safe_details() == {
        "kind": "verification_required",
        "http_status": 403,
        "platform_code": 40352,
        "request_method": "POST",
        "request_target": "mp.sohu.com/api/submit",
        "redirect_target": "mp.sohu.com/login",
    }
    assert failure.as_dict()["message"] == "password=secret raw response body"
    assert "secret" not in failure.user_message()
    assert "403" in failure.user_message()
    assert "40352" in failure.user_message()
    assert "安全验证" in failure.user_message()


@pytest.mark.parametrize("code", [True, {"password": "secret"}, "secret", "1211&token=secret", "9" * 100])
def test_failure_safe_details_drop_untrusted_metadata(code):
    failure = HTTPFailure(
        "secret", kind="unknown secret", status_code="200 secret", code=code,
        request_method="GET\r\nAuthorization: secret", request_target={"secret": "value"},
    )
    assert failure.safe_details() == {"kind": "http_error"}
    assert "secret" not in failure.user_message()


@pytest.mark.parametrize("code", [1211, "1211", -9106])
def test_failure_safe_details_accept_only_safe_numeric_codes(code):
    assert HTTPFailure("secret", code=code).safe_details()["platform_code"] == code


def test_failure_safe_details_remove_sensitive_path_values():
    failure = HTTPFailure(
        "secret",
        request_target="mp.sohu.com/api/token/short-secret/account?password=secret",
        redirect_target="https://mp.sohu.com/account/scan/login/qr-secret?token=secret",
    )
    assert failure.safe_details()["request_target"] == "mp.sohu.com/api/token/<redacted>/account"
    assert failure.safe_details()["redirect_target"] == "mp.sohu.com/account/scan/login/<redacted>"


@pytest.mark.parametrize("status,kind", [(401, "authentication_required"), (403, "platform_rejected"), (503, "http_error")])
def test_http_failure_includes_request_stage_without_request_secrets(status, kind):
    response = SimpleNamespace(status_code=status, headers={}, json=lambda: {"message": "secret"})
    http = HTTP({"mp.sohu.com"}, session=SimpleNamespace(request=lambda *a, **k: response))
    with pytest.raises(HTTPFailure) as caught:
        http.request("POST", "https://mp.sohu.com/api/submit?password=secret")
    assert caught.value.safe_details() == {
        "kind": kind, "http_status": status, "request_method": "POST",
        "request_target": "mp.sohu.com/api/submit",
    }


@pytest.mark.parametrize("exception,kind", [(requests.Timeout("secret"), "network_timeout"), (requests.ConnectionError("secret"), "network_error")])
def test_network_failure_reports_safe_request_stage(exception, kind):
    def request(*_args, **_kwargs):
        raise exception

    http = HTTP({"mp.sohu.com"}, session=SimpleNamespace(request=request))
    with pytest.raises(HTTPFailure) as caught:
        http.request("GET", "https://mp.sohu.com/api/account?token=secret")
    assert caught.value.safe_details() == {
        "kind": kind, "request_method": "GET", "request_target": "mp.sohu.com/api/account",
    }
    assert "secret" not in caught.value.user_message()


@pytest.mark.parametrize("payload", [None, ["secret"], {"error": {"code": 40352, "message": "secret"}}])
def test_http_json_failure_retains_safe_request_context(payload):
    def read_json():
        if payload is None:
            raise ValueError("secret")
        return payload

    response = SimpleNamespace(status_code=200, json=read_json)
    http = HTTP({"mp.sohu.com"}, session=SimpleNamespace(request=lambda *a, **k: response))
    with pytest.raises(HTTPFailure) as caught:
        http.json("GET", "https://mp.sohu.com/api/account?token=secret")
    details = caught.value.safe_details()
    assert details["kind"] == ("verification_required" if isinstance(payload, dict) else "invalid_response")
    assert details["http_status"] == 200
    assert details["request_method"] == "GET"
    assert details["request_target"] == "mp.sohu.com/api/account"
    assert "secret" not in caught.value.user_message()


@pytest.mark.parametrize("payload,kind,code", [
    ({"code": 1211, "msg": "password=secret"}, "authentication_required", 1211),
    ({"code": 1300, "msg": "password=secret"}, "verification_required", 1300),
    ({"code": 5000000, "msg": "password=secret"}, "platform_rejected", 5000000),
    ({"msg": "password=secret"}, "invalid_response", None),
    (["password=secret"], "invalid_response", None),
])
def test_sohu_account_reports_business_reason_instead_of_guessing_expired_login(payload, kind, code):
    client = SohuWeb(account_id="123")
    try:
        client.http.request = lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: payload)
        with pytest.raises(HTTPFailure) as caught:
            client.account()
    finally:
        client.close()
    details = caught.value.safe_details()
    assert details["kind"] == kind
    assert details.get("platform_code") == code
    assert details["http_status"] == 200
    assert details["request_method"] == "GET"
    assert details["request_target"] == ACC_API.removeprefix("https://")
    assert "secret" not in caught.value.user_message()


@pytest.mark.parametrize("data", [["secret"], {"data": "secret"}, {"data": [{"accounts": "secret"}]}])
def test_sohu_account_malformed_success_payload_is_not_expired_login(data):
    client = SohuWeb(account_id="123")
    try:
        client.http.request = lambda *a, **k: SimpleNamespace(
            status_code=200, json=lambda: {"code": 2000000, "data": data})
        with pytest.raises(HTTPFailure) as caught:
            client.account()
    finally:
        client.close()
    assert caught.value.safe_details() == {
        "kind": "invalid_response", "http_status": 200, "request_method": "GET",
        "request_target": ACC_API.removeprefix("https://"),
    }


@pytest.mark.parametrize("kind", ["authentication_required", "verification_required", "network_error", "account_mismatch"])
def test_publish_preflight_failure_is_definitely_unsubmitted(tmp_path, monkeypatch, kind):
    article = Article("标题", "正文", tmp_path / "cover.jpg")
    store = StorageLayout(tmp_path)

    def load(*_args, **_kwargs):
        raise HTTPFailure("password=secret raw failure", kind=kind, code=1211)

    monkeypatch.setattr(cli, "_load_client", load)
    result = cli._do_publish("sohu", article, store)
    assert result.status == "failed"
    assert result.platform == "sohu"
    assert result.verification == kind
    assert result.url is None
    assert "尚未提交" in result.message
    assert "secret" not in result.message
    assert not list(store.results_dir.glob("*.json"))


@pytest.mark.parametrize("error", [
    HTTPFailure("password=secret", kind="authentication_required", code=1211),
    FileNotFoundError("password=secret"),
    PermissionError("password=secret"),
    ValueError("password=secret"),
])
def test_cli_does_not_mark_exception_after_submission_definitely_failed(tmp_path, monkeypatch, capsys, error):
    source = tmp_path / "article.md"
    (tmp_path / "cover.jpg").write_bytes(b"cover")
    source.write_text("<!-- title: 标题 -->\n<!-- cover: cover.jpg -->\n正文。", encoding="utf-8")

    def publish(article, *, checkpoint, **_kwargs):
        checkpoint("submitted", "123456")
        raise error

    client = SimpleNamespace(publish=publish, close=lambda: None)
    monkeypatch.setattr(cli, "_load_client", lambda *a, **k: client)
    args = SimpleNamespace(platform="sohu", article=str(source), proxy=None)
    assert cli._cmd_publish(args, StorageLayout(tmp_path)) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "pending"
    assert result["id"] == "123456"
    assert "勿重发" in result["message"]
    assert "secret" not in result["message"]


@pytest.mark.parametrize("error", [FileNotFoundError("password=secret"), PermissionError("password=secret"), ValueError("password=secret")])
def test_publish_local_credential_failure_is_definitely_unsubmitted(tmp_path, monkeypatch, error):
    def load(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(cli, "_load_client", load)
    result = cli._do_publish("sohu", Article("标题", "正文", tmp_path / "cover.jpg"), StorageLayout(tmp_path))
    assert result.status == "failed"
    assert result.verification == "local_state_invalid"
    assert "尚未提交" in result.message
    assert "secret" not in result.message


@pytest.mark.parametrize("submitted", [False, True])
def test_sohu_publish_failure_reports_stage_and_never_raw_exception(tmp_path, submitted):
    article = Article("标题", "正文", tmp_path / "cover.jpg")
    client = SohuWeb(account_id="123")
    failure = HTTPFailure("password=secret", kind="verification_required", code=1300)

    def fail(*_args, **_kwargs):
        raise failure

    client.account = (lambda: {"id": "123"}) if submitted else fail
    client.upload_image = lambda _path: "https://img.example.test/cover.jpg"
    client.http.request = fail
    try:
        result = client.publish(article)
    finally:
        client.close()
    assert result.status == ("pending" if submitted else "failed")
    assert result.verification == ("unavailable" if submitted else "verification_required")
    assert ("勿重发" if submitted else "尚未提交") in result.message
    assert "secret" not in result.message


@pytest.mark.parametrize("payload,status,verification", [
    ({"code": 1300, "msg": "password=secret"}, "failed", "verification_required"),
    ({"code": 1211, "msg": "password=secret"}, "failed", "authentication_required"),
    ({"code": 5000000, "msg": "password=secret"}, "failed", "rejected"),
    ({"msg": "password=secret"}, "pending", "unavailable"),
])
def test_sohu_submission_response_never_exposes_raw_message_or_guesses_rejection(tmp_path, payload, status, verification):
    client = SohuWeb(account_id="123")
    client.account = lambda: {"id": "123"}
    client.upload_image = lambda _path: "https://img.example.test/cover.jpg"
    client.http.request = lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: payload)
    try:
        result = client.publish(Article("标题", "正文", tmp_path / "cover.jpg"))
    finally:
        client.close()
    assert result.status == status
    assert result.verification == verification
    assert "secret" not in result.message
    if status == "pending":
        assert "勿重发" in result.message
    else:
        assert str(payload["code"]) in result.message


@pytest.mark.parametrize("kind,status,phrase", [
    ("verification_required", "needs_login", "安全验证"),
    ("authentication_required", "needs_login", "登录态已失效"),
    ("platform_rejected", "needs_login", "拒绝"),
    ("network_timeout", "unreachable", "超时"),
    ("invalid_response", "unreachable", "无法识别"),
])
def test_session_probe_preserves_safe_reason_and_existing_status(tmp_path, monkeypatch, kind, status, phrase):
    store = StorageLayout(tmp_path)
    store.credentials("sohu").write_text("{}", encoding="utf-8")
    failure = HTTPFailure("password=secret", kind=kind, status_code=200, code=1211,
                          request_method="GET", request_target=ACC_API + "?password=secret")

    def load(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(cli, "_load_client", load)
    result = cli._probe_credential("sohu", store)
    assert result["status"] == status
    assert result["kind"] == kind
    assert phrase in result["message"]
    assert result["http_status"] == 200
    assert result["platform_code"] == 1211
    assert result["request_target"] == ACC_API.removeprefix("https://")
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("error", [FileNotFoundError("password=secret"), PermissionError("password=secret"), ValueError("password=secret")])
def test_session_probe_local_credential_errors_are_safe_and_actionable(tmp_path, monkeypatch, error):
    store = StorageLayout(tmp_path)
    store.credentials("sohu").write_text("{}", encoding="utf-8")

    def load(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(cli, "_load_client", load)
    result = cli._probe_credential("sohu", store)
    assert result["status"] == "needs_login"
    assert result["kind"] == "local_state_invalid"
    assert "登录信息" in result["message"]
    assert "secret" not in json.dumps(result)
