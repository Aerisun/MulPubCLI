"""Rechecking must preserve evidence, links, and uncertain list entries."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mulpubcli import __main__ as cli
from mulpubcli.core import Article, PublishResult, content_fingerprint
from mulpubcli.ledger import ResultLedger
from mulpubcli.platforms.xiaohongshu.client import XHSHTTP
from mulpubcli.platforms.zhihu.client import ZhihuWeb
from mulpubcli.platforms.toutiao.client import ToutiaoWeb
from mulpubcli.platforms.netease.client import NeteaseWeb
from mulpubcli.platforms.netease.browser_publish import NeteaseBrowserPublish
from mulpubcli.http import HTTPFailure
from mulpubcli.storage import StorageLayout


def _article(tmp_path: Path) -> Article:
    (tmp_path / "cover.jpg").write_bytes(b"cover")
    path = tmp_path / "article.md"
    path.write_text("<!-- title: 测试文章 -->\n\n<!-- cover: cover.jpg -->\n\n正文。", encoding="utf-8")
    return Article.load(path)


def test_url_id_ignores_query_and_netease_html_suffix():
    assert cli._url_id("https://zhuanlan.zhihu.com/p/123456?x=1") == "123456"
    assert cli._url_id("https://www.163.com/dy/article/L8DBTPLM0556PYDT.html") == "L8DBTPLM0556PYDT"


def test_xhs_statuses_keep_share_token_and_follow_pages():
    client = XHSHTTP.__new__(XHSHTTP)
    calls = []

    def posted(page=0):
        calls.append(page)
        if page == 0:
            return {"data": {"notes": [{"id": "a" * 24, "display_title": "较新",
                                        "tab_status": 1, "permission_code": 0,
                                        "xsec_token": "token-A", "xsec_source": "pc_feed"}],
                             "page": 2}}
        return {"data": {"notes": [{"id": "b" * 24, "display_title": "较旧",
                                    "tab_status": 1, "permission_code": 0,
                                    "xsec_token": "token-B", "xsec_source": "pc_feed"}],
                         "page": 0}}

    client.posted = posted
    result = client.statuses()
    assert calls == [0, 2]
    assert result["complete"] is True
    assert [note["xsec_token"] for note in result["notes"]] == ["token-A", "token-B"]


def test_xhs_minus_one_page_marks_end_of_real_creator_feed():
    client = XHSHTTP.__new__(XHSHTTP)
    pages = []

    def posted(page=0):
        pages.append(page)
        return {"data": {"notes": [{"id": "a" * 24, "display_title": "已发布",
                                   "xsec_token": "share-token", "xsec_source": "pc_feed"}],
                         "page": -1}}

    client.posted = posted
    result = client.statuses()
    assert pages == [0]
    assert result["complete"] is True
    assert result["notes"][0]["xsec_token"] == "share-token"


def test_xhs_missing_from_complete_feed_requires_explicit_deleted_code():
    client = XHSHTTP.__new__(XHSHTTP)
    client.statuses = lambda: {'notes': [], 'complete': True}
    client.detail = lambda *_a: (_ for _ in ()).throw(
        HTTPFailure('该笔记已被删除', code=-9106))
    result = client.verify('a' * 24)
    assert result.status == 'deleted'
    assert result.url is None
    assert '-9106' in result.message


def test_xhs_list_returns_openable_tokenized_link(tmp_path, monkeypatch):
    note_id = "a" * 24
    fake = SimpleNamespace(
        statuses=lambda: {"notes": [{"id": note_id, "display_title": "测试",
                                     "tab_status": 1, "permission_code": 0,
                                     "xsec_token": "a+b", "xsec_source": "pc_feed"}],
                          "complete": True},
        close=lambda: None,
    )
    monkeypatch.setattr(cli, "_load_client", lambda *a, **k: fake)
    result = cli._list_platform("xiaohongshu", StorageLayout(tmp_path))
    assert result["items"][0]["url"].endswith("?xsec_token=a%2Bb&xsec_source=pc_feed")


def test_xhs_list_without_share_token_does_not_claim_direct_link(tmp_path, monkeypatch):
    note_id = "a" * 24
    monkeypatch.setattr(cli, "_load_client", lambda *a, **k: SimpleNamespace(
        statuses=lambda: {"notes": [{"id": note_id, "display_title": "测试",
                                     "tab_status": 1, "permission_code": 0}], "complete": True},
        close=lambda: None))
    item = cli._list_platform("xiaohongshu", StorageLayout(tmp_path))["items"][0]
    assert item["url"] is None
    assert "令牌" in item["check"]


def test_xhs_old_bare_url_is_not_presented_as_openable_link(tmp_path):
    store = StorageLayout(tmp_path)
    (store.results_dir / "xiaohongshu-old.json").write_text(json.dumps({
        "platform": "xiaohongshu", "remote_id": "a" * 24, "status": "pending",
        "url": "https://www.xiaohongshu.com/explore/" + "a" * 24}))
    item = cli._ledger_items("xiaohongshu", store)[0]
    assert item["url"] is None
    assert "令牌" in item["check"]


def test_xhs_single_recheck_without_share_token_returns_no_link():
    client = XHSHTTP.__new__(XHSHTTP)
    client.statuses = lambda: {"notes": [{"id": "a" * 24, "tab_status": 2}], "complete": True}
    result = client.verify("a" * 24)
    assert result.status == "pending"
    assert result.url is None


def test_netease_draft_list_uses_editor_not_public_link(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_load_client", lambda *a, **k: SimpleNamespace(
        list_articles=lambda: [{"id": "L8DBTPLM0556PYDT", "title": "测试", "status": "draft"}],
        close=lambda: None))
    item = cli._list_platform("netease", StorageLayout(tmp_path))["items"][0]
    assert item["url"] == "https://mp.163.com/subscribe_v4/index.html#/article-publish/L8DBTPLM0556PYDT"


def test_refresh_counts_old_draft_without_article_id_as_unverified(tmp_path, monkeypatch):
    store = StorageLayout(tmp_path)
    (store.results_dir / "netease-old.json").write_text(json.dumps({
        "platform": "netease", "status": "draft", "title": "旧草稿"}))
    monkeypatch.setattr(cli, "_list_platform", lambda *a, **k: {
        "source": "live", "complete": True, "items": []})
    result = cli._refresh_platform("netease", store, proxy=None)
    assert result["draft"] == []
    assert len(result["unverified"]) == 1
    assert "缺少文章 ID" in result["unverified"][0]["check"]


def test_netease_restricted_published_state_is_visible():
    assert cli._netease_status("published(分发受限)") == "published"


def test_refresh_does_not_call_missing_ledger_article_deleted(tmp_path, monkeypatch):
    store = StorageLayout(tmp_path)
    path = store.results_dir / "toutiao-existing.json"
    path.write_text(json.dumps({"platform": "toutiao", "remote_id": "123",
                                "status": "published", "title": "旧文章",
                                "url": "https://www.toutiao.com/article/456/"}))
    monkeypatch.setattr(cli, "_list_platform", lambda *a, **k: {
        "source": "live", "complete": False, "items": []})
    result = cli._refresh_live("toutiao", store, proxy=None)
    assert result["deleted"] == []
    assert json.loads(path.read_text())["status"] == "published"


def test_uploaded_media_checkpoint_keeps_all_images_without_remote_article_id(tmp_path):
    article = _article(tmp_path)
    ledger = ResultLedger(StorageLayout(tmp_path).results_dir)
    ledger.checkpoint("zhihu", article, "uploaded", "cover-key")
    ledger.checkpoint("zhihu", article, "uploaded", "body-key")
    record = json.loads(ledger._path("zhihu", article).read_text())
    assert record["content_check"]["media"] == ["cover-key", "body-key"]
    assert "remote_id" not in record


def test_xhs_image_proof_survives_checkpoint_and_later_recheck(tmp_path):
    article = _article(tmp_path)
    ledger = ResultLedger(StorageLayout(tmp_path).results_dir)
    ledger.checkpoint("xiaohongshu", article, "uploaded", {
        "id": "upload-one", "pixel_sha256": "pixel-one", "profile": [100, 50, 1024]})
    ledger.checkpoint("xiaohongshu", article, "submitted", "a" * 24)
    proof = ledger.verification_evidence("xiaohongshu", "a" * 24)
    assert proof["media"] == ["upload-one"]
    assert proof["media_pixels"] == ["pixel-one"]
    assert proof["media_profiles"] == [[100, 50, 1024]]
    ledger.reconcile("xiaohongshu", "a" * 24, PublishResult(
        "published", "图片元数据已核对", platform="xiaohongshu", verification="published"),
        evidence=proof)
    again = ledger.verification_evidence("xiaohongshu", "a" * 24)
    assert again["media_pixels"] == ["pixel-one"]
    assert again["media_profiles"] == [[100, 50, 1024]]


def test_verification_hash_uses_published_body_without_markdown_images(tmp_path):
    (tmp_path / "body.jpg").write_bytes(b"image")
    (tmp_path / "article.md").write_text(
        "<!-- title: 测试文章 -->\n<!-- cover: cover.jpg -->\n正文前。\n![图](body.jpg)\n正文后。")
    (tmp_path / "cover.jpg").write_bytes(b"cover")
    article = Article.load(tmp_path / "article.md")
    ledger = ResultLedger(StorageLayout(tmp_path).results_dir)
    ledger.checkpoint("zhihu", article, "submitted", "123456")
    with_article = ledger.verification_evidence("zhihu", "123456", article)
    without_article = ledger.verification_evidence("zhihu", "123456")
    from mulpubcli.core import content_fingerprint, strip_markdown_images
    expected = content_fingerprint("zhihu", article.title, strip_markdown_images(article.body))
    assert with_article["sha256"] == expected["sha256"]
    assert without_article.get("sha256") != content_fingerprint("zhihu", article.title, article.body)["sha256"]


def test_pending_recheck_keeps_known_direct_link(tmp_path):
    article = _article(tmp_path)
    ledger = ResultLedger(StorageLayout(tmp_path).results_dir)
    ledger.checkpoint("zhihu", article, "submitted", "123456")
    ledger.save("zhihu", article, PublishResult(
        "published", "已发表", "https://zhuanlan.zhihu.com/p/123456", "zhihu", "verified"))
    ledger.reconcile("zhihu", "123456", PublishResult("pending", "回查暂不可用",
                                                       platform="zhihu", verification="unavailable"))
    record = json.loads(ledger._path("zhihu", article).read_text())
    assert record["status"] == "published"
    assert record["url"] == "https://zhuanlan.zhihu.com/p/123456"


def test_pending_to_pending_recheck_keeps_known_direct_link(tmp_path):
    article = _article(tmp_path)
    ledger = ResultLedger(StorageLayout(tmp_path).results_dir)
    ledger.checkpoint("zhihu", article, "submitted", "123456")
    ledger.save("zhihu", article, PublishResult(
        "pending", "审核中", "https://zhuanlan.zhihu.com/p/123456", "zhihu", "unavailable"))
    ledger.reconcile("zhihu", "123456", PublishResult(
        "pending", "回查暂不可用", platform="zhihu", verification="unavailable"))
    assert json.loads(ledger._path("zhihu", article).read_text())["url"] == \
        "https://zhuanlan.zhihu.com/p/123456"


def test_recheck_does_not_replace_dedup_hash_with_rendered_body_hash(tmp_path):
    article = _article(tmp_path)
    ledger = ResultLedger(StorageLayout(tmp_path).results_dir)
    ledger.checkpoint("zhihu", article, "submitted", "123456")
    path = ledger._path("zhihu", article)
    before = json.loads(path.read_text())["content_check"]["sha256"]
    ledger.reconcile("zhihu", "123456", PublishResult(
        "published", "已核验", "https://zhuanlan.zhihu.com/p/123456", "zhihu", "verified"),
        evidence={"sha256": "rendered-hash", "version": 1})
    assert json.loads(path.read_text())["content_check"]["sha256"] == before


def test_legacy_url_only_record_can_be_reconciled_by_verified_article_id(tmp_path):
    article = _article(tmp_path)
    ledger = ResultLedger(StorageLayout(tmp_path).results_dir)
    ledger.save("zhihu", article, PublishResult(
        "published", "旧记录", "https://zhuanlan.zhihu.com/p/123456", "zhihu", "published"))
    proof = ledger.verification_evidence("zhihu", "123456")
    assert proof["sha256"]
    assert ledger.reconcile("zhihu", "123456", PublishResult(
        "published", "重新核验", "https://zhuanlan.zhihu.com/p/123456", "zhihu", "verified"),
        evidence=proof)
    record = json.loads(ledger._path("zhihu", article).read_text())
    assert record["remote_id"] == "123456"
    assert record["status"] == "published"


def test_zhihu_published_without_local_media_proof_still_returns_article_link():
    client = ZhihuWeb.__new__(ZhihuWeb)
    client.http = SimpleNamespace(json=lambda *a, **k: {
        "id": 123456, "state": "published", "title": "测试文章", "content": "<p>正文。</p>"})
    result = client.verify("123456")
    assert result.status == "published"
    assert result.verification == "published"
    assert result.url == "https://zhuanlan.zhihu.com/p/123456"


def test_zhihu_public_404_checks_own_draft_before_saying_missing():
    client = ZhihuWeb.__new__(ZhihuWeb)

    def get(_method, url):
        if url.endswith("/draft"):
            return {"id": 123456, "title": "测试文章", "content": "<p>正文。</p>"}
        raise HTTPFailure("not found", status_code=404)

    client.http = SimpleNamespace(json=get)
    assert client.article_state("123456") == "draft"
    result = client.verify("123456")
    assert result.status == "draft"
    assert result.url == "https://zhuanlan.zhihu.com/p/123456/edit"


def test_zhihu_two_authenticated_404s_mark_tracked_article_deleted():
    client = ZhihuWeb.__new__(ZhihuWeb)
    client.http = SimpleNamespace(json=lambda *_a, **_k: (_ for _ in ()).throw(
        HTTPFailure('not found', status_code=404)))
    result = client.verify('123456')
    assert result.status == 'deleted'
    assert result.url is None
    assert '404' in result.message


def test_toutiao_explicit_pgc_delete_in_editor_detail():
    client = ToutiaoWeb.__new__(ToutiaoWeb)
    client.account_id = '1'
    client.account = lambda: {'id': '1'}
    client.find_article = lambda *_a, **_k: None
    client.detail = lambda *_a, **_k: {
        'pgc_id': '123456', 'media_id': '1',
        'article_pgc': {'status': 4,
                        'extra': '{"visibility_level_reason":"pgc_delete"}'}}
    result = client.verify('123456')
    assert result.status == 'deleted'
    assert result.url is None
    assert 'pgc_delete' in result.message


def test_netease_owner_detail_post_state_seven_marks_deleted():
    client = NeteaseWeb.__new__(NeteaseWeb)
    client.wemedia_id = 'W123'
    client.account = lambda: {'id': 'W123'}
    client._find = lambda *_a, **_k: None
    client.article_detail = lambda *_a, **_k: {
        'docid': 'L8EIRMO70556PYDT', 'wemediaId': 'W123', 'postState': 7}
    result = client.verify('L8EIRMO70556PYDT')
    assert result.status == 'deleted'
    assert result.url is None
    assert '7' in result.message


def test_zhihu_refresh_preserves_uncertain_article_in_list(tmp_path, monkeypatch):
    article = _article(tmp_path)
    store = StorageLayout(tmp_path)
    ledger = ResultLedger(store.results_dir)
    ledger.checkpoint("zhihu", article, "submitted", "123456")
    ledger.save("zhihu", article, PublishResult(
        "published", "已发布", "https://zhuanlan.zhihu.com/p/123456", "zhihu", "published"))
    fake = SimpleNamespace(article_state=lambda _id: "not_found")
    monkeypatch.setattr(cli, "_load_client", lambda *a, **k: fake)
    result = cli._refresh_zhihu(store, proxy=None)
    assert result["deleted"] == []
    assert result["unverified"][0]["url"] == "https://zhuanlan.zhihu.com/p/123456"
    assert json.loads(ledger._path("zhihu", article).read_text())["status"] == "published"


def test_list_uses_ledger_link_when_live_service_is_unavailable(tmp_path, monkeypatch, capsys):
    store = StorageLayout(tmp_path)
    path = store.results_dir / "xiaohongshu-existing.json"
    direct = "https://www.xiaohongshu.com/explore/" + "a" * 24 + "?xsec_token=shared"
    path.write_text(json.dumps({"platform": "xiaohongshu", "remote_id": "a" * 24,
                                "status": "published", "title": "旧文章", "url": direct}))
    monkeypatch.setattr(cli, "_load_client", lambda *a, **k: (_ for _ in ()).throw(
        FileNotFoundError("凭证暂不可用")))
    code = cli._cmd_list(SimpleNamespace(platform="xiaohongshu", json=True, proxy=None), store)
    result = json.loads(capsys.readouterr().out)["platforms"]["xiaohongshu"]
    assert code == 0
    assert result["source"] == "ledger"
    assert result["items"][0]["url"] == direct
    assert result["items"][0]["status"] == "unreachable"
    assert "凭证暂不可用" in result["items"][0]["check"]


def test_verify_refresh_json_contains_each_article_link(tmp_path, monkeypatch, capsys):
    store = StorageLayout(tmp_path)
    item = {"platform": "toutiao", "id": "123", "title": "测试", "status": "published",
            "url": "https://www.toutiao.com/article/456/"}
    monkeypatch.setattr(cli, "_refresh_platform", lambda *a, **k: {
        "published": [item], "draft": [], "other": [], "unverified": [], "deleted": []})
    cli._cmd_verify_refresh(("toutiao",), store, proxy=None, as_json=True)
    result = json.loads(capsys.readouterr().out)
    assert result["articles"]["toutiao"][0]["url"] == item["url"]


def test_human_list_shows_direct_links():
    shown = cli._list_human({"toutiao": {"source": "live", "items": [{
        "id": "123", "title": "测试", "status": "published",
        "url": "https://www.toutiao.com/article/456/"}]}})
    assert "https://www.toutiao.com/article/456/" in shown


def test_human_list_and_single_verify_show_reason_for_missing_link():
    item = {"platform": "xiaohongshu", "id": "a" * 24, "title": "测试",
            "status": "pending", "url": None, "check": "缺少分享令牌，暂不能生成可直接打开的链接"}
    assert "缺少分享令牌" in cli._list_human({"xiaohongshu": {"source": "live", "items": [item]}})
    assert "回查原因" in cli._render_single({**item, "message": "等待平台提供分享令牌"})
    assert "等待平台提供分享令牌" in cli._render_single({**item, "message": "等待平台提供分享令牌"})


def test_xhs_pending_recheck_keeps_tokenized_direct_link():
    client = XHSHTTP.__new__(XHSHTTP)
    note_id = "a" * 24
    client.statuses = lambda: {"notes": [{"id": note_id, "tab_status": 2,
                                           "xsec_token": "shared", "xsec_source": "pc_feed"}],
                               "complete": True}
    result = client.verify(note_id)
    assert result.status == "pending"
    assert result.url.endswith("?xsec_token=shared&xsec_source=pc_feed")


def test_xhs_verifies_rewritten_image_ids_by_original_pixels_in_order():
    client = XHSHTTP.__new__(XHSHTTP)
    note_id = "a" * 24
    client.statuses = lambda: {"notes": [{"id": note_id, "tab_status": 1,
                                           "permission_code": 0, "xsec_token": "shared"}],
                               "complete": True}
    client.detail = lambda _id: {"data": {"id": note_id, "enabled": True,
                                           "privacy": {"type": 0}, "title": "标题", "desc": "正文",
                                           "images_list": [{"fileid": "spectrum/new-one", "original": "one"},
                                                           {"fileid": "spectrum/new-two", "original": "two"}]}}
    client._remote_image_hash = lambda url: {"one": "hash-one", "two": "hash-two"}[url]
    evidence = {**content_fingerprint("xiaohongshu", "标题", "正文"),
                "media": ["old-one", "old-two"], "media_pixels": ["hash-one", "hash-two"]}
    result = client.verify(note_id, evidence=evidence)
    assert result.status == "published"
    assert result.verification == "verified"
    assert "xsec_token=shared" in result.url

    client._remote_image_hash = lambda url: {"one": "hash-two", "two": "hash-one"}[url]
    result = client.verify(note_id, evidence=evidence)
    assert result.status == "pending"
    assert result.verification == "mismatch"


def test_xhs_uses_upload_profiles_when_cdn_original_is_unavailable():
    client = XHSHTTP.__new__(XHSHTTP)
    note_id = "a" * 24
    client.statuses = lambda: {"notes": [{"id": note_id, "tab_status": 1,
                                           "permission_code": 0, "xsec_token": "shared"}],
                               "complete": True}
    client.detail = lambda _id: {"data": {"id": note_id, "enabled": True,
                                           "privacy": {"type": 0}, "title": "标题", "desc": "正文",
                                           "images_list": [
                                               {"fileid": "spectrum/new-one", "original": "one",
                                                "width": 100, "height": 50,
                                                "metadata": {"origin_size": 1024 / 1024}},
                                               {"fileid": "spectrum/new-two", "original": "two",
                                                "width": 200, "height": 100,
                                                "metadata": {"origin_size": 2048 / 1024}}]}}
    client._remote_image_hash = lambda _url: (_ for _ in ()).throw(HTTPFailure("CDN 暂不可达"))
    evidence = {**content_fingerprint("xiaohongshu", "标题", "正文"),
                "media": ["old-one", "old-two"], "media_pixels": ["hash-one", "hash-two"],
                "media_profiles": [[100, 50, 1024], [200, 100, 2048]]}
    result = client.verify(note_id, evidence=evidence)
    assert result.status == "published"
    assert result.verification == "published"
    assert "未做像素核对" in result.message
    evidence["media_profiles"] = [[200, 100, 2048], [100, 50, 1024]]
    result = client.verify(note_id, evidence=evidence)
    assert result.status == "pending"


def test_toutiao_pending_recheck_returns_public_item_link():
    client = ToutiaoWeb.__new__(ToutiaoWeb)
    client.account = lambda: None
    client.find_article = lambda _id, **kwargs: {"pgc_id": "123", "item_id": "456", "status": 1}
    result = client.verify("123")
    assert result.status == "pending"
    assert result.url == "https://www.toutiao.com/article/456/"


def test_toutiao_content_mismatch_still_returns_article_link():
    client = ToutiaoWeb.__new__(ToutiaoWeb)
    client.account = lambda: None
    client.account_id = "999"
    client.find_article = lambda _id, **kwargs: {"pgc_id": "123", "item_id": "456", "status": 2}
    client.detail = lambda _id: {"pgc_id": "123", "media_id": "999", "title": "别的文章",
                                 "content": "<p>不匹配</p>", "pgc_feed_covers": []}
    result = client.verify("123", evidence={"version": 1, "sha256": "expected",
                                            "media": ["cover"]})
    assert result.status == "pending"
    assert result.verification == "mismatch"
    assert result.url == "https://www.toutiao.com/article/456/"


def test_toutiao_checkpoints_cover_and_body_media(tmp_path):
    (tmp_path / "body.jpg").write_bytes(b"image")
    (tmp_path / "article.md").write_text(
        "<!-- title: 测试文章 -->\n<!-- cover: cover.jpg -->\n正文。\n![图](body.jpg)")
    (tmp_path / "cover.jpg").write_bytes(b"cover")
    article = Article.load(tmp_path / "article.md")
    client = ToutiaoWeb.__new__(ToutiaoWeb)
    client.account = lambda: None
    client.mobile_bound = True
    client.account_id = "999"
    client.upload_image = lambda path: {"url": f"https://example.com/{path.name}",
                                        "uri": path.name, "width": 1, "height": 1}
    client.prepare_csrf = lambda: None
    client.call = lambda *a, **k: {"pgc_id": "123"}
    client.verify = lambda *a, **k: PublishResult("pending", "待审核", platform="toutiao")
    events = []
    client.publish(article, checkpoint=lambda stage, value: events.append((stage, value)))
    assert events == [("uploaded", "cover.jpg"), ("uploaded", "body.jpg"),
                      ("submitted", "123")]


def test_xhs_checkpoints_cover_and_body_media(tmp_path):
    (tmp_path / "body.jpg").write_bytes(b"image")
    (tmp_path / "article.md").write_text(
        "<!-- title: 测试文章 -->\n<!-- cover: cover.jpg -->\n正文。\n![图](body.jpg)")
    (tmp_path / "cover.jpg").write_bytes(b"cover")
    article = Article.load(tmp_path / "article.md")
    client = XHSHTTP.__new__(XHSHTTP)
    client.upload_image = lambda path: {"file_id": path.name, "width": 1, "height": 1, "size": 1}
    client.call = lambda *a, **k: {"data": {"id": "a" * 24}}
    client.verify = lambda *a, **k: PublishResult("pending", "待审核", platform="xiaohongshu")
    events = []
    client.publish(article, checkpoint=lambda stage, value: events.append((stage, value)))
    assert events == [("uploaded", "cover.jpg"), ("uploaded", "body.jpg"),
                      ("submitted", "a" * 24)]


def test_netease_restricted_article_recheck_has_public_link():
    client = NeteaseWeb.__new__(NeteaseWeb)
    client.account = lambda: None
    client._find = lambda _id: {"articleId": "L8DBTPLM0556PYDT", "contentState": 3,
                               "unrecomReason": "该内容分发受限", "title": "测试"}
    result = client.verify("L8DBTPLM0556PYDT")
    assert result.status == "published"
    assert result.url == "https://www.163.com/dy/article/L8DBTPLM0556PYDT.html"


def test_netease_draft_recheck_returns_editor_link():
    client = NeteaseWeb.__new__(NeteaseWeb)
    client.account = lambda: None
    client._find = lambda _id: {"articleId": "L8DBTPLM0556PYDT", "contentState": 0}
    result = client.verify("L8DBTPLM0556PYDT")
    assert result.status == "draft"
    assert result.url == "https://mp.163.com/subscribe_v4/index.html#/article-publish/L8DBTPLM0556PYDT"


def test_zhihu_draft_list_uses_editor_link(tmp_path, monkeypatch):
    article = _article(tmp_path)
    store = StorageLayout(tmp_path)
    ledger = ResultLedger(store.results_dir)
    ledger.checkpoint("zhihu", article, "draft", "123456")
    ledger.save("zhihu", article, PublishResult("draft", "草稿", platform="zhihu"))
    monkeypatch.setattr(cli, "_load_client", lambda *a, **k: SimpleNamespace(
        verify=lambda _id, **kwargs: PublishResult(
            "draft", "草稿仍在", "https://zhuanlan.zhihu.com/p/123456/edit", "zhihu", "published"),
        close=lambda: None))
    item = cli._list_platform("zhihu", store)["items"][0]
    assert item["url"] == "https://zhuanlan.zhihu.com/p/123456/edit"


def test_zhihu_list_reports_content_mismatch_instead_of_only_published_state(tmp_path, monkeypatch):
    article = _article(tmp_path)
    store = StorageLayout(tmp_path)
    ledger = ResultLedger(store.results_dir)
    ledger.checkpoint("zhihu", article, "submitted", "123456")
    ledger.save("zhihu", article, PublishResult(
        "published", "平台已发表", "https://zhuanlan.zhihu.com/p/123456", "zhihu", "published"))
    monkeypatch.setattr(cli, "_load_client", lambda *a, **k: SimpleNamespace(
        verify=lambda _id, **kwargs: PublishResult(
            "pending", "文章已存在，但图片未通过核对", "https://zhuanlan.zhihu.com/p/123456",
            "zhihu", "mismatch"), close=lambda: None))
    item = cli._list_platform("zhihu", store)["items"][0]
    assert item["status"] == "pending"
    assert item["verification"] == "mismatch"
    assert "图片未通过核对" in item["check"]
    assert item["url"] == "https://zhuanlan.zhihu.com/p/123456"


def test_netease_browser_success_checkpoints_returned_article_id(tmp_path, monkeypatch):
    article = _article(tmp_path)
    store = StorageLayout(tmp_path)
    credential = store.credentials("netease")
    credential.write_text(json.dumps({"cookies": [{"name": "session", "value": "test"}]}))
    monkeypatch.setattr(NeteaseBrowserPublish, "run", lambda *a, **k: {
        "status": "ok", "remote_id": "L8DBTPLM0556PYDT", "message": "已提交"})
    cli._netease_browser_publish(article, store)
    record = json.loads(ResultLedger(store.results_dir)._path("netease", article).read_text())
    assert record["remote_id"] == "L8DBTPLM0556PYDT"


@pytest.mark.parametrize("platform,article_id,url", [
    ("xiaohongshu", "a" * 24, "https://www.xiaohongshu.com/explore/" + "a" * 24 + "?xsec_token=shared"),
    ("zhihu", "123456", "https://zhuanlan.zhihu.com/p/123456"),
    ("toutiao", "123456", "https://www.toutiao.com/article/789/"),
    ("netease", "L8DBTPLM0556PYDT", "https://www.163.com/dy/article/L8DBTPLM0556PYDT.html"),
])
def test_refresh_fully_rechecks_saved_article_and_returns_link(
        tmp_path, monkeypatch, platform, article_id, url):
    article = _article(tmp_path)
    store = StorageLayout(tmp_path)
    ledger = ResultLedger(store.results_dir)
    ledger.checkpoint(platform, article, "submitted", article_id)
    ledger.save(platform, article, PublishResult("pending", "待核验", platform=platform))
    seen = []

    def verify(found_id, **kwargs):
        seen.append((found_id, kwargs.get("evidence")))
        return PublishResult("published", "已核验", url, platform, "verified")

    monkeypatch.setattr(cli, "_load_client", lambda *a, **k: SimpleNamespace(
        verify=verify, close=lambda: None))
    if platform != "zhihu":
        monkeypatch.setattr(cli, "_list_platform", lambda *a, **k: {
            "source": "live", "complete": True, "items": []})
    result = cli._refresh_platform(platform, store, proxy=None)
    assert seen and seen[0][0] == article_id
    assert result["published"][0]["url"] == url
    assert result["published"][0]["verification"] == "verified"
    record = json.loads(ledger._path(platform, article).read_text())
    assert record["status"] == "published"
    assert record["url"] == url


def test_toutiao_list_reaches_articles_after_five_pages():
    client = ToutiaoWeb.__new__(ToutiaoWeb)
    client.user_id = "user"
    pages = []

    def feed(_method, _path, **kwargs):
        page = int(json.loads(kwargs["params"]["client_extra_params"])["page_index"])
        pages.append(page)
        pgc = str(1000 + page)
        content = {"article_attr": {"pgc_cell": json.dumps({
            "pgc_id": pgc, "item_id": str(page + 1000), "title": "测试"}),
            "status": 2, "create_time": 0}}
        return {"errno": 20100, "login_status": 1, "message": "success",
                "data": [{"content": json.dumps(content)}], "has_more": page < 6}

    client.call = feed
    rows = client.list_articles()
    assert pages == [1, 2, 3, 4, 5, 6]
    assert rows[-1]["id"] == "1006"


def test_netease_single_article_search_reaches_after_ten_pages():
    client = NeteaseWeb.__new__(NeteaseWeb)
    target = "L8DBTPLM0556PYDT"
    seen = []

    def page(*, content_state, page_no, **kwargs):
        seen.append((content_state, page_no))
        if content_state == 0:
            return []
        if content_state == 3 and page_no <= 10:
            return [{"articleId": f"OLDER{page_no:04d}{i:04d}", "contentState": 3}
                    for i in range(20)]
        if content_state == 3 and page_no == 11:
            return [{"articleId": target, "contentState": 3}]
        return []

    client._list_page = page
    assert client.article_state(target) == "published"
    assert (3, 11) in seen


def test_toutiao_list_without_item_id_does_not_invent_public_link(tmp_path, monkeypatch):
    fake = SimpleNamespace(list_articles=lambda: [{"id": "123", "title": "测试",
                                                    "status": 2, "item_id": None}], close=lambda: None)
    monkeypatch.setattr(cli, "_load_client", lambda *a, **k: fake)
    result = cli._list_platform("toutiao", StorageLayout(tmp_path))
    assert result["items"][0]["url"] is None


def test_publish_passes_checkpoint_to_adapter(tmp_path, monkeypatch):
    article = _article(tmp_path)
    store = StorageLayout(tmp_path)

    def publish(_article, *, checkpoint, public):
        checkpoint("submitted", "123456")
        return PublishResult("pending", "待审核", platform="toutiao")

    monkeypatch.setattr(cli, "_load_client", lambda *a, **k: SimpleNamespace(publish=publish))
    cli._do_publish("toutiao", article, store)
    record = json.loads(ResultLedger(store.results_dir)._path("toutiao", article).read_text())
    assert record["remote_id"] == "123456"
    assert record["stage"] == "submitted"


def test_verify_id_uses_article_evidence_updates_ledger_and_returns_link(tmp_path, monkeypatch, capsys):
    article = _article(tmp_path)
    store = StorageLayout(tmp_path)
    ledger = ResultLedger(store.results_dir)
    ledger.checkpoint("zhihu", article, "submitted", "123456")
    seen = {}

    def verify(article_id, *, expected=None, evidence=None):
        seen.update(id=article_id, expected=expected, evidence=evidence)
        return PublishResult("published", "已核验", "https://zhuanlan.zhihu.com/p/123456",
                             "zhihu", "verified")

    monkeypatch.setattr(cli, "_load_client", lambda *a, **k: SimpleNamespace(verify=verify))
    args = SimpleNamespace(id="123456", platform="zhihu", article=str(tmp_path / "article.md"), json=True,
                           proxy=None)
    code = cli._cmd_verify(args, store)
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert seen["expected"].title == article.title
    assert seen["evidence"]["sha256"]
    assert output["url"] == "https://zhuanlan.zhihu.com/p/123456"
    record = json.loads(ledger._path("zhihu", article).read_text())
    assert record["status"] == "published"
    assert record["url"] == output["url"]


def test_verify_id_uses_toutiao_draft_feed_for_saved_draft(tmp_path, monkeypatch, capsys):
    article = _article(tmp_path)
    store = StorageLayout(tmp_path)
    ledger = ResultLedger(store.results_dir)
    ledger.checkpoint("toutiao", article, "draft", "123456")
    ledger.save("toutiao", article, PublishResult("draft", "草稿已保存", platform="toutiao"))
    seen = []

    def verify(_article_id, *, draft, **kwargs):
        seen.append(draft)
        return PublishResult("draft", "头条草稿已确认", platform="toutiao", verification="verified")

    monkeypatch.setattr(cli, "_load_client", lambda *a, **k: SimpleNamespace(verify=verify))
    code = cli._cmd_verify(SimpleNamespace(id="123456", platform="toutiao", article=None,
                                           json=True, proxy=None), store)
    assert code == 0
    assert seen == [True]
