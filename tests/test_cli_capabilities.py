"""CLI commands expose only actions their platform adapters can perform."""

import json
from types import SimpleNamespace

import pytest

from mulpubcli import __main__ as cli
from mulpubcli.core import Article, PublishResult
from mulpubcli.platforms.zhihu.client import ZhihuWeb
from mulpubcli.platforms.sohu.client import SohuWeb
from mulpubcli.storage import StorageLayout


def _article(tmp_path):
    (tmp_path / "cover.jpg").write_bytes(b"cover")
    path = tmp_path / "article.md"
    path.write_text("# 测试标题\n<!-- cover: cover.jpg -->\n正文。", encoding="utf-8")
    return path, Article.load(path)


def test_sohu_publish_reports_the_submitted_id(tmp_path, monkeypatch, capsys):
    path, article = _article(tmp_path)
    store = StorageLayout(tmp_path)
    def submit(*_args, **_kwargs):
        from mulpubcli.ledger import ResultLedger
        ResultLedger(store.results_dir).checkpoint("sohu", article, "submitted", "42")
        return PublishResult("pending", "搜狐已接收图文投稿", platform="sohu")
    monkeypatch.setattr(cli, "_do_publish", submit)
    args = SimpleNamespace(platform="sohu", article=str(path), proxy=None)

    assert cli._cmd_publish(args, store) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "pending"
    assert result["platform"] == "sohu"
    assert result["id"] == "42"


def test_sohu_adapter_publish_calls_public_endpoint_without_saving_draft(tmp_path):
    _, article = _article(tmp_path)
    client = SohuWeb.__new__(SohuWeb)
    client.account_id = "123"
    client.account = lambda: {"id": "123"}
    client.upload_image = lambda path: f"https://cdn.example.com/{path.name}"
    client._write_headers = lambda: {}
    requests = []
    def request(method, url, **kwargs):
        requests.append((method, url, kwargs["json"]))
        return SimpleNamespace(json=lambda: {"code": 2000000, "data": 42})
    client.http = SimpleNamespace(request=request)
    checkpoints = []

    result = client.publish(article, checkpoint=lambda stage, value: checkpoints.append((stage, value)))
    assert result.status == "pending"
    assert result.platform == "sohu"
    assert requests[0][1].endswith("/news/publish/v2?accountId=123")
    assert requests[0][2]["creationStatement"] == 0
    assert checkpoints[-1] == ("submitted", "42")


def test_sohu_publish_uses_requested_creation_statement(tmp_path):
    _, article = _article(tmp_path)
    client = SohuWeb.__new__(SohuWeb)
    client.account_id = "123"
    client.account = lambda: {"id": "123"}
    client.upload_image = lambda path: f"https://cdn.example.com/{path.name}"
    client._write_headers = lambda: {}
    sent = {}
    def request(_method, _url, **kwargs):
        sent.update(kwargs["json"])
        return SimpleNamespace(json=lambda: {"code": 2000000, "data": 42})
    client.http = SimpleNamespace(request=request)

    client.publish(article, declaration="fiction")
    assert sent["creationStatement"] == 1


def test_sohu_public_endpoint_originality_challenge_stays_pending(tmp_path):
    _, article = _article(tmp_path)
    client = SohuWeb.__new__(SohuWeb)
    client.account_id = "123"
    client.account = lambda: {"id": "123"}
    client.upload_image = lambda path: f"https://cdn.example.com/{path.name}"
    client._write_headers = lambda: {}
    client.http = SimpleNamespace(request=lambda *_a, **_k: SimpleNamespace(json=lambda: {
        "code": 2000000, "data": {"id": 42, "failureType": 0}}))
    checkpoints = []

    result = client.publish(article, checkpoint=lambda stage, value: checkpoints.append((stage, value)))

    assert result.status == "pending"
    assert checkpoints[-1] == ("submitted", "42")


def test_sohu_declaration_option_reaches_adapter(tmp_path, monkeypatch):
    _, article = _article(tmp_path)
    store = StorageLayout(tmp_path)
    selected = {}
    fake = SimpleNamespace(
        publish=lambda _article, **kwargs: (
            selected.update(kwargs) or PublishResult("pending", "submitted", platform="sohu")),
        close=lambda: None)
    monkeypatch.setattr(cli, "_load_client", lambda *_a, **_k: fake)
    cli._do_publish("sohu", article, store, declaration="fiction")
    assert selected["declaration"] == "fiction"


def test_sohu_list_maps_review_and_public_states():
    client = SohuWeb.__new__(SohuWeb)
    client.account_id = "456"
    client.dv_id = "device"
    client.sp_cm = "client"
    client.account = lambda: {"id": "456"}
    requests = []
    def request(method, url, **kwargs):
        requests.append((method, url, kwargs.get("params"), kwargs.get("headers")))
        return SimpleNamespace(json=lambda: {"code": 2000000, "data": {
            "news": [
                {"id": 11, "title": "审核中", "status": 2, "userId": 456},
                {"id": 12, "title": "已发布", "status": 4, "userId": 456},
                {"id": 13, "title": "草稿", "status": 1, "userId": 456},
            ], "totalCount": 3}})
    client.http = SimpleNamespace(request=request)

    rows = client.list_articles()

    assert [r["status"] for r in rows] == ["pending", "published", "draft"]
    assert rows[1]["url"] == "https://www.sohu.com/a/12_456"
    assert requests[0][2]["newsType"] == 1
    assert requests[0][3]["dv-id"] == "device"
    assert requests[0][3]["sp-cm"] == "client"
    assert client._last_list_complete is True


def test_sohu_list_reads_tracked_article_detail_when_filtered_feed_omits_it(
        tmp_path, monkeypatch, capsys):
    _, article = _article(tmp_path)
    store = StorageLayout(tmp_path)
    from mulpubcli.ledger import ResultLedger
    ResultLedger(store.results_dir).checkpoint("sohu", article, "submitted", "42")
    client = SohuWeb.__new__(SohuWeb)
    client.account_id = "456"
    client.account = lambda: {"id": "456"}
    client._write_headers = lambda: {}
    requested = []

    def request(method, url, **kwargs):
        requested.append((method, url, kwargs.get("params")))
        if url.endswith("/users/news"):
            payload = {"code": 2000000, "data": {"news": [], "totalCount": 0}}
        else:
            payload = {"code": 2000000, "data": {"news": {
                "id": 42, "userId": 456, "title": article.title, "status": 7,
                "deletedBy": 1, "postTime": 1791184144000}}}
        return SimpleNamespace(json=lambda: payload)

    client.http = SimpleNamespace(request=request)
    monkeypatch.setattr(cli, "_load_client", lambda *_a, **_k: client)
    assert cli._cmd_list(SimpleNamespace(platform="sohu", json=True, proxy=None), store) == 0
    item = json.loads(capsys.readouterr().out)["platforms"]["sohu"]["items"][0]
    assert item["status"] == "deleted"
    assert item["url"] is None
    assert "下架" in item["check"]
    assert any(url.endswith("/article") and params.get("newsId") == "42"
               for _, url, params in requested)


def test_sohu_verify_keeps_reviewing_article_pending():
    client = SohuWeb.__new__(SohuWeb)
    client.list_articles = lambda: [{"id": "42", "title": "测试标题", "status": "pending",
                                     "url": None}]
    result = client.verify("42")
    assert result.status == "pending"
    assert result.verification == "unavailable"


def test_sohu_verify_reports_explicit_deleted_detail_when_feed_omits_article():
    client = SohuWeb.__new__(SohuWeb)
    client.list_articles = lambda: []
    client.article_detail = lambda article_id: {
        "id": article_id, "title": "测试标题", "status": "deleted", "url": None,
        "check": "搜狐详情显示文章已下架或删除（状态码 7）；公开链接不可访问"}
    result = client.verify("42")
    assert result.status == "deleted"
    assert result.url is None
    assert "状态码 7" in result.message


def test_deleted_result_clears_public_url_but_keeps_tracked_record(tmp_path, monkeypatch, capsys):
    _, article = _article(tmp_path)
    store = StorageLayout(tmp_path)
    from mulpubcli.ledger import ResultLedger
    ledger = ResultLedger(store.results_dir)
    ledger.checkpoint('sohu', article, 'submitted', '42')
    ledger.save('sohu', article, PublishResult('published', '已发布',
                'https://www.sohu.com/a/42_456', 'sohu', 'verified'))
    fake = SimpleNamespace(verify=lambda *_a, **_k: PublishResult(
        'deleted', '搜狐详情状态码 7', platform='sohu', verification='verified'),
        close=lambda: None)
    monkeypatch.setattr(cli, '_load_client', lambda *_a, **_k: fake)
    assert cli._cmd_verify_one('42', 'sohu', store, proxy=None, as_json=True) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'deleted'
    assert result['url'] is None
    item = cli._ledger_items('sohu', store)[0]
    assert item['status'] == 'deleted'
    assert item['url'] is None
    assert ledger.reconcile('sohu', '42', PublishResult(
        'pending', '本次详情暂不可读', platform='sohu', verification='unavailable'))
    assert cli._ledger_items('sohu', store)[0]['status'] == 'deleted'
    fake.verify = lambda *_a, **_k: PublishResult(
        'pending', '本次详情暂不可读', platform='sohu', verification='unavailable')
    assert cli._cmd_verify_one('42', 'sohu', store, proxy=None, as_json=True) == 1
    retry = json.loads(capsys.readouterr().out)
    assert retry['status'] == 'deleted'
    assert retry['url'] is None


@pytest.mark.parametrize('platform,remote_id', [
    ('netease', 'L8EIRMO70556PYDT'), ('toutiao', '7692401631350047232')])
def test_list_reads_missing_tracked_article_and_persists_explicit_deletion(
        platform, remote_id, tmp_path, monkeypatch, capsys):
    _, article = _article(tmp_path)
    store = StorageLayout(tmp_path)
    from mulpubcli.ledger import ResultLedger
    ledger = ResultLedger(store.results_dir)
    ledger.checkpoint(platform, article, 'submitted', remote_id)
    ledger.save(platform, article, PublishResult(
        'published', '已发布', f'https://example.com/{remote_id}', platform, 'published'))
    fake = SimpleNamespace(list_articles=lambda: [], verify=lambda *_a, **_k: PublishResult(
        'deleted', '单篇详情明确显示已删除', platform=platform, verification='verified'),
        _last_list_complete=True, close=lambda: None)
    monkeypatch.setattr(cli, '_load_client', lambda *_a, **_k: fake)
    assert cli._cmd_list(SimpleNamespace(platform=platform, json=True, proxy=None), store) == 0
    item = json.loads(capsys.readouterr().out)['platforms'][platform]['items'][0]
    assert item['status'] == 'deleted'
    assert item['url'] is None
    assert cli._ledger_items(platform, store)[0]['status'] == 'deleted'


def test_xhs_list_persists_explicit_deleted_code(tmp_path, monkeypatch, capsys):
    _, article = _article(tmp_path)
    store = StorageLayout(tmp_path)
    from mulpubcli.ledger import ResultLedger
    ledger = ResultLedger(store.results_dir)
    note_id = 'a' * 24
    ledger.checkpoint('xiaohongshu', article, 'submitted', note_id)
    ledger.save('xiaohongshu', article, PublishResult(
        'published', '曾发布', 'https://www.xiaohongshu.com/explore/' + note_id + '?xsec_token=old',
        'xiaohongshu', 'published'))
    fake = SimpleNamespace(statuses=lambda: {'notes': [], 'complete': True},
                           verify=lambda *_a, **_k: PublishResult(
                               'deleted', '详情返回 -9106：该笔记已被删除',
                               platform='xiaohongshu', verification='verified'),
                           close=lambda: None)
    monkeypatch.setattr(cli, '_load_client', lambda *_a, **_k: fake)
    assert cli._cmd_list(SimpleNamespace(platform='xiaohongshu', json=True, proxy=None), store) == 0
    item = json.loads(capsys.readouterr().out)['platforms']['xiaohongshu']['items'][0]
    assert item['status'] == 'deleted'
    assert item['url'] is None
    assert cli._ledger_items('xiaohongshu', store)[0]['status'] == 'deleted'


def test_list_auth_error_displays_unreachable_instead_of_stale_published(
        tmp_path, monkeypatch, capsys):
    _, article = _article(tmp_path)
    store = StorageLayout(tmp_path)
    from mulpubcli.ledger import ResultLedger
    from mulpubcli.http import HTTPFailure
    ledger = ResultLedger(store.results_dir)
    ledger.checkpoint('xiaohongshu', article, 'submitted', 'a' * 24)
    ledger.save('xiaohongshu', article, PublishResult(
        'published', '曾发布', 'https://www.xiaohongshu.com/explore/' + 'a' * 24 + '?xsec_token=old',
        'xiaohongshu', 'published'))
    monkeypatch.setattr(cli, '_load_client', lambda *_a, **_k: (_ for _ in ()).throw(
        HTTPFailure('HTTP 401', status_code=401)))
    assert cli._cmd_list(SimpleNamespace(platform='xiaohongshu', json=True, proxy=None), store) == 0
    item = json.loads(capsys.readouterr().out)['platforms']['xiaohongshu']['items'][0]
    assert item['status'] == 'unreachable'
    assert 'HTTP 401' in item['check']


def test_verify_refresh_keeps_deleted_article_in_tracking_output(tmp_path, monkeypatch, capsys):
    item = {'id': '42', 'title': '测试标题', 'status': 'deleted', 'url': None,
            'check': '单篇详情显示已删除'}
    monkeypatch.setattr(cli, '_refresh_platform', lambda *_a, **_k: {
        'published': [], 'draft': [], 'deleted': [item], 'other': [], 'unverified': []})
    assert cli._cmd_verify_refresh(('sohu',), StorageLayout(tmp_path), proxy=None, as_json=True) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['refresh']['sohu']['deleted'] == 1
    assert result['articles']['sohu'] == [item]


def test_verify_refresh_401_does_not_display_stale_published_status(tmp_path, monkeypatch):
    _, article = _article(tmp_path)
    store = StorageLayout(tmp_path)
    from mulpubcli.ledger import ResultLedger
    from mulpubcli.http import HTTPFailure
    ledger = ResultLedger(store.results_dir)
    ledger.checkpoint('xiaohongshu', article, 'submitted', 'a' * 24)
    ledger.save('xiaohongshu', article, PublishResult(
        'published', '曾发布', 'https://www.xiaohongshu.com/explore/' + 'a' * 24 + '?xsec_token=old',
        'xiaohongshu', 'published'))
    monkeypatch.setattr(cli, '_list_platform', lambda *_a, **_k: {
        'source': 'ledger', 'complete': False, 'items': []})
    monkeypatch.setattr(cli, '_load_client', lambda *_a, **_k: SimpleNamespace(
        verify=lambda *_a, **_k: (_ for _ in ()).throw(HTTPFailure('HTTP 401', status_code=401)),
        close=lambda: None))
    result = cli._refresh_verified('xiaohongshu', store, proxy=None)
    assert result['unverified'][0]['status'] == 'unreachable'
    assert 'HTTP 401' in result['unverified'][0]['check']
    assert cli._ledger_items('xiaohongshu', store)[0]['status'] == 'published'


def test_sohu_verify_checks_published_body_against_original(tmp_path):
    _, article = _article(tmp_path)
    client = SohuWeb.__new__(SohuWeb)
    client.account_id = "456"
    client.dv_id = "device"
    client.sp_cm = "client"
    client.list_articles = lambda: [{"id": "42", "title": article.title,
                                     "status": "published", "url": "https://www.sohu.com/a/42_456"}]
    client.http = SimpleNamespace(request=lambda *a, **k: SimpleNamespace(json=lambda: {
        "code": 2000000, "data": {"news": {"id": 42, "title": article.title,
                                             "content": "<img src='https://cdn.example.com/cover.jpg'><p>正文。</p>"}}}))

    result = client.verify("42", expected=article,
                           evidence={"media": ["https://cdn.example.com/cover.jpg"]})

    assert result.status == "published"
    assert result.verification == "verified"


def test_sohu_rewritten_cdn_image_url_keeps_published_with_partial_check(tmp_path):
    _, article = _article(tmp_path)
    client = SohuWeb.__new__(SohuWeb)
    client.account_id = '456'
    client.list_articles = lambda: [{'id': '42', 'title': article.title,
                                     'status': 'published', 'url': 'https://www.sohu.com/a/42_456'}]
    client._write_headers = lambda: {}
    client.http = SimpleNamespace(request=lambda *_a, **_k: SimpleNamespace(json=lambda: {
        'code': 2000000, 'data': {'news': {'id': 42, 'title': article.title,
            'cover': 'https://q1.itc.cn/rewritten.jpg', 'content': '<p>正文。</p>'}}}))
    result = client.verify('42', expected=article,
                           evidence={'media': ['https://res.mp.sohu.com/upload.jpg']})
    assert result.status == 'published'
    assert result.verification == 'published'
    assert '图片' in result.message


def test_sohu_verify_does_not_claim_full_match_without_media_evidence(tmp_path):
    _, article = _article(tmp_path)
    client = SohuWeb.__new__(SohuWeb)
    client.account_id = "456"
    client.dv_id = "device"
    client.sp_cm = "client"
    client.list_articles = lambda: [{"id": "42", "title": article.title,
                                     "status": "published", "url": "https://www.sohu.com/a/42_456"}]
    client.http = SimpleNamespace(request=lambda *a, **k: SimpleNamespace(json=lambda: {
        "code": 2000000, "data": {"news": {"id": 42, "title": article.title,
                                             "content": "<p>正文。</p>"}}}))
    assert client.verify("42", expected=article).verification == "published"


def test_sohu_public_url_recovers_only_the_article_id():
    assert cli._url_id("https://www.sohu.com/a/42_456") == "42"
    assert cli._url_id("https://www.sohu.com/a/not-an-id_456") is None
    from mulpubcli.ledger import ResultLedger
    assert ResultLedger._id_from_url("sohu", "https://www.sohu.com/a/42_456") == "42"


@pytest.mark.parametrize("platform", ["xiaohongshu", "sohu"])
def test_unsupported_draft_uses_the_same_json_result(tmp_path, capsys, platform):
    path, _ = _article(tmp_path)
    code = cli.main(["--root", str(tmp_path), "draft", platform, "--article", str(path)])
    assert code == 2
    result = json.loads(capsys.readouterr().out)
    assert result["platform"] == platform
    assert result["status"] == "failed"
    assert result["verification"] == "unsupported"


def test_publish_help_does_not_offer_unused_urs_token(capsys):
    with pytest.raises(SystemExit):
        cli.main(["publish", "--help"])
    assert "--urs-token" not in capsys.readouterr().out


def test_draft_help_names_the_supported_platforms(capsys):
    with pytest.raises(SystemExit):
        cli.main(["draft", "--help"])
    help_text = capsys.readouterr().out
    assert "仅支持 zhihu、toutiao、netease" in help_text


def test_zhihu_draft_saves_and_reports_the_remote_id_without_publishing(tmp_path):
    _, article = _article(tmp_path)
    client = ZhihuWeb.__new__(ZhihuWeb)
    client.account = lambda: {"id": "account"}
    client.create_draft = lambda _article, **kwargs: (
        kwargs["checkpoint"]("123456789") or "123456789")
    client._publish_saved_draft = lambda *a, **k: pytest.fail("public submit")
    checkpoints = []

    result = client.draft(article, checkpoint=lambda stage, value: checkpoints.append((stage, value)))

    assert result == PublishResult(
        "draft", "知乎草稿已保存并回读一致",
        "https://zhuanlan.zhihu.com/p/123456789/edit", "zhihu", "verified")
    assert checkpoints == [("draft", "123456789")]


def test_sohu_readback_uses_same_list_and_verify_entrypoints(tmp_path, monkeypatch, capsys):
    store = StorageLayout(tmp_path)
    (store.results_dir / "sohu-tracked.json").write_text(json.dumps({
        "platform": "sohu", "remote_id": "123", "title": "跟踪文章", "status": "pending"}))
    calls = []
    monkeypatch.setattr(cli, "_list_platform", lambda platform, *_a, **_k: (
        calls.append(("list", platform)) or {"source": "live", "complete": True, "items": [
            {"id": "123", "title": "跟踪文章", "status": "published",
             "url": "https://www.sohu.com/a/123_456"}]}))
    monkeypatch.setattr(cli, "_load_client", lambda platform, *_a, **_k: SimpleNamespace(
        verify=lambda article_id, **_k: (
            calls.append(("verify", platform, article_id)) or
            PublishResult("published", "已发布", "https://www.sohu.com/a/123_456", "sohu", "published")),
        close=lambda: None))
    list_args = SimpleNamespace(platform="sohu", json=True, proxy=None)
    assert cli._cmd_list(list_args, store) == 0
    listed = json.loads(capsys.readouterr().out)["platforms"]["sohu"]
    assert listed["source"] == "live"

    verify_args = SimpleNamespace(id="123", platform="sohu", article=None, json=True, proxy=None)
    assert cli._cmd_verify(verify_args, store) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["status"] == "published"
    assert checked["platform"] == "sohu"
    assert ("verify", "sohu", "123") in calls


def test_default_readback_skips_platform_calls_without_tracked_articles(tmp_path, monkeypatch, capsys):
    store = StorageLayout(tmp_path)
    seen = []

    def listing(platform, *_a, **_k):
        seen.append(platform)
        return {"source": "live", "complete": True, "items": []}

    monkeypatch.setattr(cli, "_list_platform", listing)
    assert cli._cmd_list(SimpleNamespace(platform=None, json=True, proxy=None), store) == 0
    assert seen == []
    capsys.readouterr()

    seen.clear()
    monkeypatch.setattr(cli, "_refresh_platform", lambda platform, *_a, **_k: (
        seen.append(platform) or {"published": [], "draft": [], "other": [],
                                   "unverified": [], "deleted": []}))
    args = SimpleNamespace(id=None, platform=None, article=None, json=True, proxy=None)
    assert cli._cmd_verify(args, store) == 0
    assert seen == ["xiaohongshu", "zhihu", "toutiao", "netease", "sohu"]
