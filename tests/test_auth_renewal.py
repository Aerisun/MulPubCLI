"""Credential renewal happens before an authenticated operation starts."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests

from mulpubcli import __main__ as cli
from mulpubcli.http import HTTPFailure, add_cookies, private_json, save_session
from mulpubcli.platforms.netease.client import NeteaseWeb
from mulpubcli.platforms.netease.login import NeteaseLogin
from mulpubcli.platforms.sohu.client import SohuWeb
from mulpubcli.platforms.sohu.login import SohuLogin
from mulpubcli.storage import StorageLayout
from mulpubcli.browser import LoginResult


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
    p_info = {"name": "P_INFO", "value": "profile", "domain": ".163.com",
              "path": "/", "secure": False, "expires": -1}
    s_info = {"name": "S_INFO", "value": "account", "domain": ".163.com",
              "path": "/", "secure": False, "expires": -1}
    late = {"name": "URS", "value": "login", "domain": "reg.163.com",
            "path": "/", "secure": False, "expires": -1}
    late_related = [
        {"name": "NTES_SESS", "value": "session-extra", "domain": ".163.com",
         "path": "/", "secure": True, "expires": -1},
        {"name": "ntes_nnid", "value": "device", "domain": "reg.163.com",
         "path": "/", "secure": False, "expires": -1},
    ]
    calls = []

    def cookies():
        calls.append(True)
        initial = [session, p_info, s_info]
        if len(calls) <= 4:
            return initial
        if len(calls) == 5:
            return [*initial, late]
        return [*initial, late, *late_related]

    page = SimpleNamespace(context=SimpleNamespace(cookies=cookies), wait_for_timeout=lambda _ms: None)
    login._capture_after_login(page)

    saved = json.loads(login.destination.read_text(encoding="utf-8"))
    assert len(calls) >= 9
    assert {item["name"] for item in saved["cookies"]} == {
        "NTESwebSI", "P_INFO", "S_INFO", "URS", "NTES_SESS", "ntes_nnid"}


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


def test_netease_refresh_carries_original_account_id(tmp_path, monkeypatch, capsys):
    store = StorageLayout(tmp_path)
    private_json(store.credentials('netease'), {'cookies': [], 'account_id': 'original'})
    seen = []

    def run(login, *, headless):
        seen.append(login.account_id)
        return {'status': 'failed', 'message': 'stopped before replacing credentials'}

    monkeypatch.setattr(NeteaseLogin, 'run', run)
    args = SimpleNamespace(phone='13800000000', password='secret', refresh=True, proxy=None)
    assert cli._netease_login(args, store) == 1
    capsys.readouterr()
    assert seen == ['original']


def test_netease_refresh_rejects_other_account_before_replacing_credentials(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from mulpubcli.browser import LoginResult

    path = tmp_path / 'netease.json'
    path.write_text('original', encoding='utf-8')
    login = NeteaseLogin('phone', 'password', path, account_id='original')

    def browser_login(*_args, **_kwargs):
        login.destination.write_text('new-session', encoding='utf-8')
        return LoginResult(status='ok')

    client = Mock()
    client.account.return_value = {'id': 'other', 'name': 'Other'}
    monkeypatch.setattr('mulpubcli.browser.PlaywrightLoginer', lambda **_kwargs: Mock(login=browser_login))
    monkeypatch.setattr(NeteaseWeb, 'load', lambda _path: client)

    result = login.run()
    assert result['status'] == 'failed'
    assert path.read_text(encoding='utf-8') == 'original'
    client.save.assert_not_called()


@pytest.mark.parametrize('platform,login_class', [
    ('netease', NeteaseLogin), ('sohu', SohuLogin)])
def test_changed_credentials_do_not_inherit_browser_session(tmp_path, platform, login_class):
    login = login_class('phone', 'password', tmp_path / f'{platform}.json',
                        fresh_browser=True)
    with patch('mulpubcli.browser.PlaywrightLoginer') as browser:
        browser.return_value.login.return_value = LoginResult(status='failed')
        assert login.run()['status'] == 'failed'
    assert browser.call_args.kwargs['profile_name'] is None


@pytest.mark.parametrize('platform,login_class', [
    ('netease', NeteaseLogin), ('sohu', SohuLogin)])
def test_refresh_without_original_account_id_stops_before_browser(
        tmp_path, monkeypatch, capsys, platform, login_class):
    store = StorageLayout(tmp_path)
    private_json(store.credentials(platform), {'cookies': []})
    browser = []
    monkeypatch.setattr(login_class, 'run', lambda *_args, **_kwargs: browser.append(True))
    args = SimpleNamespace(phone='13800000000', password='secret', refresh=True,
                           proxy=None, show_browser=False)
    command = cli._netease_login if platform == 'netease' else cli._sohu_login
    assert command(args, store) == 1
    capsys.readouterr()
    assert browser == []
    assert not store.login_secret(platform).exists()


@pytest.mark.parametrize('platform,login_class', [
    ('netease', NeteaseLogin), ('sohu', SohuLogin)])
@pytest.mark.parametrize('new_phone,new_password', [
    ('13900000000', 'old-secret'), ('13800000000', 'new-secret')])
def test_refresh_verifies_changed_login_pair_in_fresh_browser(
        tmp_path, monkeypatch, capsys, platform, login_class, new_phone, new_password):
    store = StorageLayout(tmp_path)
    private_json(store.credentials(platform), {'cookies': [], 'account_id': 'original'})
    private_json(store.login_secret(platform),
                 {'phone': '13800000000', 'password': 'old-secret'})
    browser = []

    def run(login, *, headless):
        browser.append((login.phone, login.password, login.account_id,
                        login.fresh_browser))
        return {'status': 'ok', 'account_id': 'original', 'username': 'author'}

    monkeypatch.setattr(login_class, 'run', run)
    args = SimpleNamespace(phone=new_phone, password=new_password, refresh=True,
                           proxy=None, show_browser=False)
    command = cli._netease_login if platform == 'netease' else cli._sohu_login
    assert command(args, store) == 0
    capsys.readouterr()
    assert browser == [(new_phone, new_password, 'original', True)]
    assert json.loads(store.login_secret(platform).read_text(encoding='utf-8')) == {
        'phone': new_phone, 'password': new_password}


@pytest.mark.parametrize('platform,login_class', [
    ('netease', NeteaseLogin), ('sohu', SohuLogin)])
def test_refresh_uses_saved_pair_without_prompting(
        tmp_path, monkeypatch, capsys, platform, login_class):
    store = StorageLayout(tmp_path)
    private_json(store.credentials(platform), {'cookies': [], 'account_id': 'original'})
    private_json(store.login_secret(platform),
                 {'phone': '13800000000', 'password': 'secret'})
    seen = []

    def run(login, *, headless):
        seen.append((login.phone, login.password, login.fresh_browser))
        return {'status': 'failed', 'message': 'stopped before replacing credentials'}

    monkeypatch.setattr(login_class, 'run', run)
    monkeypatch.setattr('builtins.input', lambda *_args: pytest.fail('unexpected phone prompt'))
    monkeypatch.setattr('getpass.getpass', lambda *_args: pytest.fail('unexpected password prompt'))
    args = SimpleNamespace(phone=None, password=None, refresh=True,
                           proxy=None, show_browser=False)
    command = cli._netease_login if platform == 'netease' else cli._sohu_login
    assert command(args, store) == 1
    capsys.readouterr()
    assert seen == [('13800000000', 'secret', False)]


@pytest.mark.parametrize('platform,login_class', [
    ('netease', NeteaseLogin), ('sohu', SohuLogin)])
def test_refresh_missing_saved_pair_fails_without_prompting(
        tmp_path, monkeypatch, capsys, platform, login_class):
    store = StorageLayout(tmp_path)
    private_json(store.credentials(platform), {'cookies': [], 'account_id': 'original'})
    monkeypatch.delenv('NETEASE_PHONE', raising=False)
    monkeypatch.delenv('NETEASE_PASS', raising=False)
    monkeypatch.delenv('SOHU_PHONE', raising=False)
    monkeypatch.delenv('SOHU_PASSWORD', raising=False)
    monkeypatch.setattr('builtins.input', lambda *_args: pytest.fail('unexpected phone prompt'))
    monkeypatch.setattr('getpass.getpass', lambda *_args: pytest.fail('unexpected password prompt'))
    browser = []
    monkeypatch.setattr(login_class, 'run', lambda *_args, **_kwargs: browser.append(True))
    args = SimpleNamespace(phone=None, password=None, refresh=True,
                           proxy=None, show_browser=False)
    command = cli._netease_login if platform == 'netease' else cli._sohu_login
    assert command(args, store) == 1
    output = capsys.readouterr().out
    assert '续期' in output
    assert browser == []


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
        if platform == 'sohu':
            assert login.allow_human is False
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
    if platform == 'netease':
        profile = store.auth_dir / 'sohu-profile'
    else:
        profile = store.auth_dir / SohuLogin._profile_name('13800000000')
    profile.mkdir()
    (profile / 'Cookies').write_bytes(b'old-account')

    assert cli._cmd_reset(SimpleNamespace(platform=platform), store) == 0
    capsys.readouterr()
    assert not store.credentials(platform).exists()
    assert not store.login_secret(platform).exists()
    assert not profile.exists()
