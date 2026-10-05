"""Credential renewal happens before an authenticated operation starts."""

import json
from types import SimpleNamespace

import pytest
import requests

from mulpubcli import __main__ as cli
from mulpubcli.http import HTTPFailure, add_cookies, private_json, save_session
from mulpubcli.platforms.netease.client import NeteaseWeb
from mulpubcli.platforms.netease.login import NeteaseLogin
from mulpubcli.platforms.sohu.client import SohuWeb
from mulpubcli.platforms.sohu.login import SohuLogin
from mulpubcli.storage import StorageLayout


def test_netease_preserves_related_163_domain_cookies(tmp_path):
    path = tmp_path / "netease.json"
    session = requests.Session()
    add_cookies(session, [
        {"name": "NTESwebSI", "value": "session", "domain": "mp.163.com", "path": "/"},
        {"name": "URS", "value": "login", "domain": "reg.163.com", "path": "/"},
    ])
    save_session(path, session)

    client = NeteaseWeb.load(path)
    try:
        client.save(path)
    finally:
        client.close()

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert {(item["name"], item["domain"]) for item in saved["cookies"]} == {
        ("NTESwebSI", "mp.163.com"), ("URS", "reg.163.com")}


def test_netease_preserves_cookie_expiry_and_drops_expired_cookies(tmp_path):
    path = tmp_path / "netease.json"
    session = requests.Session()
    add_cookies(session, [
        {"name": "NTESwebSI", "value": "session", "domain": "mp.163.com",
         "path": "/", "expires": 2_000_000_000},
        {"name": "old", "value": "expired", "domain": "reg.163.com",
         "path": "/", "expires": 1},
    ])
    save_session(path, session)

    client = NeteaseWeb.load(path)
    try:
        client.save(path)
    finally:
        client.close()

    cookies = json.loads(path.read_text(encoding="utf-8"))["cookies"]
    assert [(item["name"], item["expires"]) for item in cookies] == [
        ("NTESwebSI", 2_000_000_000)]


def test_netease_renewal_credential_rejects_another_account(tmp_path):
    path = tmp_path / "netease.json"
    session = requests.Session()
    add_cookies(session, [{"name": "NTESwebSI", "value": "session",
                           "domain": "mp.163.com", "path": "/"}])
    save_session(path, session, account_id="original")

    client = NeteaseWeb.load(path)
    client.http.json = lambda *_args, **_kwargs: {
        "code": 1, "data": {"wemediaId": "different", "tname": "other"}}
    try:
        with pytest.raises(HTTPFailure) as failure:
            client.account()
    finally:
        client.close()

    assert failure.value.kind == "account_mismatch"
    assert json.loads(path.read_text(encoding="utf-8"))["account_id"] == "original"


def test_netease_export_waits_for_late_related_cookie(tmp_path):
    login = NeteaseLogin("phone", "password", tmp_path / "netease.json")
    session = {"name": "NTESwebSI", "value": "session", "domain": "mp.163.com",
               "path": "/", "secure": False, "expires": -1}
    late = {"name": "URS", "value": "login", "domain": "reg.163.com",
            "path": "/", "secure": False, "expires": -1}
    calls = []

    def cookies():
        calls.append(True)
        return [session] if len(calls) <= 2 else [session, late]

    page = SimpleNamespace(context=SimpleNamespace(cookies=cookies), wait_for_timeout=lambda _ms: None)
    login._capture_after_login(page)

    saved = json.loads(login.destination.read_text(encoding="utf-8"))
    assert len(calls) >= 3
    assert {item["name"] for item in saved["cookies"]} == {"NTESwebSI", "URS"}


def test_verified_netease_login_saves_phone_and_password_separately(tmp_path, monkeypatch, capsys):
    store = StorageLayout(tmp_path)

    def run(login, *, headless):
        private_json(login.destination, {"cookies": [], "account_id": "creator"})
        return {"status": "ok", "account_id": "creator", "username": "author"}

    monkeypatch.setattr(NeteaseLogin, "run", run)
    args = SimpleNamespace(phone="13800000000", password="secret", refresh=True, proxy=None)

    assert cli._netease_login(args, store) == 0
    capsys.readouterr()
    secret = store.login_secret("netease")
    assert secret.stat().st_mode & 0o777 == 0o600
    assert json.loads(secret.read_text(encoding="utf-8")) == {
        "phone": "13800000000", "password": "secret"}
    assert "password" not in json.loads(store.credentials("netease").read_text(encoding="utf-8"))


def test_verified_sohu_login_saves_phone_and_password_separately(tmp_path, monkeypatch, capsys):
    store = StorageLayout(tmp_path)

    def run(login, *, headless):
        private_json(login.destination, {"cookies": [], "account_id": "creator"})
        return {"status": "ok", "account_id": "creator", "username": "author"}

    monkeypatch.setattr(SohuLogin, "run", run)
    args = SimpleNamespace(phone="13800000000", password="secret", refresh=True,
                           proxy=None, show_browser=False)

    assert cli._sohu_login(args, store) == 0
    capsys.readouterr()
    secret = store.login_secret("sohu")
    assert secret.stat().st_mode & 0o777 == 0o600
    assert json.loads(secret.read_text(encoding="utf-8")) == {
        "phone": "13800000000", "password": "secret"}
    assert "password" not in json.loads(store.credentials("sohu").read_text(encoding="utf-8"))


class _AccountClient:
    def __init__(self, result):
        self.result = result
        self.closed = False

    def account(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self):
        self.closed = True


def test_expired_netease_credential_is_renewed_before_operation(tmp_path, monkeypatch):
    store = StorageLayout(tmp_path)
    private_json(store.credentials("netease"), {"cookies": []})
    old = _AccountClient(HTTPFailure("expired", kind="authentication_required"))
    fresh = _AccountClient({"id": "creator"})
    loaded = iter([old, fresh])
    monkeypatch.setattr(NeteaseWeb, "load", lambda _path: next(loaded))
    renewals = []
    monkeypatch.setattr(cli, "_auto_renew_credentials",
                        lambda platform, _store, **_kwargs: renewals.append(platform), raising=False)

    assert cli._load_client("netease", store) is fresh
    assert old.closed is True
    assert renewals == ["netease"]


def test_expired_sohu_credential_is_renewed_before_operation(tmp_path, monkeypatch):
    store = StorageLayout(tmp_path)
    private_json(store.credentials("sohu"), {"cookies": []})
    old = _AccountClient(HTTPFailure("expired", kind="authentication_required"))
    fresh = _AccountClient({"id": "creator"})
    loaded = iter([old, fresh])
    monkeypatch.setattr(SohuWeb, "load", lambda _path: next(loaded))
    renewals = []
    monkeypatch.setattr(cli, "_auto_renew_credentials",
                        lambda platform, _store, **_kwargs: renewals.append(platform), raising=False)

    assert cli._load_client("sohu", store) is fresh
    assert old.closed is True
    assert renewals == ["sohu"]


def test_network_failure_does_not_trigger_password_login(tmp_path, monkeypatch):
    store = StorageLayout(tmp_path)
    private_json(store.credentials("netease"), {"cookies": []})
    client = _AccountClient(HTTPFailure("offline", kind="network_error"))
    monkeypatch.setattr(NeteaseWeb, "load", lambda _path: client)
    renewals = []
    monkeypatch.setattr(cli, "_auto_renew_credentials",
                        lambda platform, _store, **_kwargs: renewals.append(platform), raising=False)

    with pytest.raises(HTTPFailure, match="offline"):
        cli._load_client("netease", store)
    assert client.closed is True
    assert renewals == []


@pytest.mark.parametrize("platform,login_class", [
    ("netease", NeteaseLogin), ("sohu", SohuLogin)])
def test_auto_renew_uses_saved_pair_and_checks_original_account(
        tmp_path, monkeypatch, platform, login_class):
    store = StorageLayout(tmp_path)
    private_json(store.credentials(platform), {"cookies": [], "account_id": "original"})
    private_json(store.login_secret(platform), {"phone": "13800000000", "password": "secret"})
    monkeypatch.setattr(cli, "_load_client_raw", lambda *_args, **_kwargs:
                        _AccountClient(HTTPFailure("expired", kind="authentication_required")))
    seen = []

    def run(login, *, headless):
        seen.append((login.phone, login.password, login.account_id, headless))
        return {"status": "ok", "account_id": "original"}

    monkeypatch.setattr(login_class, "run", run)
    cli._auto_renew_credentials(platform, store)
    assert seen == [("13800000000", "secret", "original", True)]


@pytest.mark.parametrize("platform", ["netease", "sohu"])
def test_reset_removes_saved_password(tmp_path, capsys, platform):
    store = StorageLayout(tmp_path)
    private_json(store.credentials(platform), {"cookies": []})
    private_json(store.login_secret(platform), {"phone": "13800000000", "password": "secret"})

    assert cli._cmd_reset(SimpleNamespace(platform=platform), store) == 0
    capsys.readouterr()
    assert not store.credentials(platform).exists()
    assert not store.login_secret(platform).exists()
