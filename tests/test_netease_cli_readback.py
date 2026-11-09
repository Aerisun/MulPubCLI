"""CLI verification reaches the adapter's narrowly scoped phone-check fallback."""

import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import requests

from mulpubcli import __main__ as cli
from mulpubcli.core import Article, PublishResult
from mulpubcli.http import HTTPFailure
from mulpubcli.ledger import ResultLedger
from mulpubcli.platforms.netease.client import NeteaseWeb
from mulpubcli.storage import StorageLayout


ARTICLE_ID = "TEST_ARTICLE_001"
ACCOUNT_ID = "test_creator"


@pytest.fixture
def phone_check_clients(monkeypatch):
    calls = []
    clients = []

    def load(platform, _store, **_kwargs):
        assert platform == "netease"
        session = requests.Session()
        session.cookies.set("NTESwebSI", "synthetic-session", domain="mp.163.com")
        client = NeteaseWeb(session=session)
        client.wemedia_id = ACCOUNT_ID
        clients.append(client)

        def request(method, url, **kwargs):
            path = urlsplit(url).path
            calls.append((method, path))
            if path == "/wemedia/navinfo.do":
                payload = {"code": 100030}
            elif path == "/wemedia/article/editpage.do":
                assert kwargs["params"]["postId"] == ARTICLE_ID
                payload = {"code": 1, "data": {"post": {
                    "docid": ARTICLE_ID, "wemediaId": ACCOUNT_ID, "postState": 3,
                }}}
            elif path == "/wemedia/content/manage/list.do":
                payload = {"code": 1, "data": {"list": []}}
            else:
                pytest.fail(f"Unexpected platform operation: {method} {path}")
            return SimpleNamespace(status_code=200, headers={}, json=lambda: payload)

        session.request = request
        return client

    monkeypatch.setattr(cli, "_load_client_raw", load)
    monkeypatch.setattr(cli, "_auto_renew_credentials", lambda *a, **k: pytest.fail("Unexpected login attempt"))
    yield calls
    for client in clients:
        client.close()


def test_single_cli_verification_reaches_owned_article_fallback(tmp_path, capsys, phone_check_clients):
    code = cli._cmd_verify_one(ARTICLE_ID, "netease", StorageLayout(tmp_path), proxy=None, as_json=True)
    result = json.loads(capsys.readouterr().out)
    assert code == 0
    assert result["status"] == "published"
    assert result["verification"] == "published"
    assert "手机号验证" in result["message"]
    assert any(path == "/wemedia/article/editpage.do" for _, path in phone_check_clients)


def test_batch_cli_verification_reaches_owned_article_fallback(tmp_path, phone_check_clients):
    store = StorageLayout(tmp_path)
    article = Article("测试标题", "测试正文", tmp_path / "cover.jpg")
    ledger = ResultLedger(store.results_dir)
    ledger.checkpoint("netease", article, "submitted", ARTICLE_ID)
    ledger.save("netease", article, PublishResult("pending", "待核验", platform="netease"))

    result = cli._refresh_verified("netease", store, proxy=None)

    assert len(result["published"]) == 1
    assert result["published"][0]["id"] == ARTICLE_ID
    assert not result["unverified"]
    assert json.loads(ledger._path("netease", article).read_text())["status"] == "published"


def test_publish_does_not_enable_phone_check_readback_exception(tmp_path, phone_check_clients):
    article = Article("测试标题", "测试正文", tmp_path / "cover.jpg")
    result = cli._do_publish("netease", article, StorageLayout(tmp_path))
    assert result.status == "failed"
    assert result.verification == "verification_required"
    assert "尚未提交" in result.message
    assert phone_check_clients == [("GET", "/wemedia/navinfo.do")]


@pytest.mark.parametrize("phone_check_after_renewal", [False, True])
def test_readback_client_still_renews_confirmed_expired_credentials(tmp_path, monkeypatch, phone_check_after_renewal):
    expired = SimpleNamespace(account=lambda: (_ for _ in ()).throw(
        HTTPFailure("expired", kind="authentication_required")), close=lambda: None)
    def account():
        if phone_check_after_renewal:
            raise HTTPFailure("phone verification", kind="verification_required", code=100030)
        return {"id": ACCOUNT_ID}

    refreshed = SimpleNamespace(account=account, wemedia_id=ACCOUNT_ID, close=lambda: None)
    clients = iter([expired, refreshed])
    renewals = []
    monkeypatch.setattr(cli, "_load_client_raw", lambda *a, **k: next(clients))
    monkeypatch.setattr(cli, "_auto_renew_credentials", lambda platform, *a, **k: renewals.append(platform))
    assert cli._load_client("netease", StorageLayout(tmp_path), readback=True) is refreshed
    assert renewals == ["netease"]


@pytest.mark.parametrize("platform,kind,code", [
    ("netease", "verification_required", 1300),
    ("netease", "platform_rejected", 100030),
    ("netease", "network_error", None),
    ("sohu", "verification_required", 100030),
])
def test_readback_client_does_not_ignore_other_platform_failures(tmp_path, monkeypatch, platform, kind, code):
    failure = HTTPFailure("synthetic", kind=kind, code=code)
    closed = []
    client = SimpleNamespace(account=lambda: (_ for _ in ()).throw(failure), close=lambda: closed.append(True))
    monkeypatch.setattr(cli, "_load_client_raw", lambda *a, **k: client)
    with pytest.raises(HTTPFailure) as caught:
        cli._load_client(platform, StorageLayout(tmp_path), readback=True)
    assert caught.value is failure
    assert closed == [True]
