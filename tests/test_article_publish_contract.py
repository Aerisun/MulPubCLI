"""Publication contract: metadata stays out of the body and images keep their order."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mulpubcli.core import Article
from mulpubcli.browser import LoginResult
from mulpubcli.http import HTTPFailure
from mulpubcli.__main__ import _cmd_publish, _netease_browser_publish, _netease_editor_media
from mulpubcli.platforms.netease.browser_publish import NeteaseBrowserPublish
from mulpubcli.platforms.netease.client import NeteaseWeb
from mulpubcli.platforms.sohu.client import SohuWeb
from mulpubcli.platforms.toutiao.client import ToutiaoWeb, article_form
from mulpubcli.platforms.zhihu.client import ZhihuWeb
from mulpubcli.platforms.xiaohongshu.client import image_payload
from mulpubcli.renderer import body_content_blocks, render
from mulpubcli.storage import StorageLayout


def _article(tmp_path: Path, *, summary: str = "一句摘要") -> Article:
    for name in ("cover.jpg", "one.jpg", "two.jpg"):
        (tmp_path / name).write_bytes(b"image")
    source = tmp_path / "article.md"
    source.write_text(
        "# 标题\n\n"
        "<!-- cover: cover.jpg -->\n\n"
        f"<!-- summary: {summary} -->\n\n"
        "开头。\n\n![第一张](one.jpg)\n\n中间。\n\n"
        "![第二张](two.jpg)\n\n结尾。\n",
        encoding="utf-8",
    )
    return Article.load(source)


def _image_map(article: Article) -> dict[str, str]:
    return {str(path): f"https://cdn.example.com/{path.name}" for path in article.all_images()}


def _in_order(text: str, *parts: str) -> None:
    positions = [text.index(part) for part in parts]
    assert positions == sorted(positions)


def test_article_extracts_metadata_without_leaking_into_body(tmp_path):
    article = _article(tmp_path)
    assert article.title == "标题"
    assert article.summary == "一句摘要"
    assert article.cover == tmp_path / "cover.jpg"
    assert [path.name for path in article.body_images] == ["one.jpg", "two.jpg"]
    assert "summary:" not in article.body
    assert "cover:" not in article.body
    assert "一句摘要" not in article.body
    assert "# 标题" not in article.body
    _in_order(article.body, "开头。", "one.jpg", "中间。", "two.jpg", "结尾。")


def test_multiline_summary_is_extracted_and_not_published_as_body(tmp_path):
    article = _article(tmp_path, summary="第一句\n第二句")
    assert article.summary == "第一句\n第二句"
    assert "第一句" not in article.body
    assert "第二句" not in article.body


@pytest.mark.parametrize("directive", [
    "<!-- summary: 又一条 -->",
    "<!-- cover: one.jpg -->",
    "<!-- summary: 没有结束",
])
def test_duplicate_or_unclosed_metadata_is_rejected(tmp_path, directive):
    for name in ("cover.jpg", "one.jpg"):
        (tmp_path / name).write_bytes(b"image")
    source = tmp_path / "article.md"
    source.write_text(
        "# 标题\n<!-- cover: cover.jpg -->\n<!-- summary: 摘要 -->\n"
        f"{directive}\n正文。",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        Article.load(source)


def test_toutiao_submits_summary_separately_and_keeps_body_image_order(tmp_path):
    article = _article(tmp_path)
    image_map = _image_map(article)
    cover = {"url": image_map[str(article.cover)], "uri": "cover-uri",
             "width": 640, "height": 480}
    form = article_form(article, cover, image_map, public=True, media_id="123")
    assert json.loads(form["search_creation_info"])["abstract"] == article.summary
    assert json.loads(form["pgc_feed_covers"]) == [cover]
    assert form["ic_uri_list"] == cover["uri"]
    assert cover["url"] not in form["content"]
    assert "一句摘要" not in form["content"]
    assert "<!--" not in form["content"]
    _in_order(form["content"], "开头。", "one.jpg", "中间。", "two.jpg", "结尾。")


def test_zhihu_submits_cover_as_draft_title_image_not_body_image(tmp_path):
    article = _article(tmp_path)
    client = ZhihuWeb.__new__(ZhihuWeb)
    uploaded = lambda path: f"https://pic1.zhimg.com/{path.name}"
    client.upload_image = uploaded
    saved = {"id": "123", "title": article.title, "content": ""}
    patches = []

    def get_or_create(method, url, **kwargs):
        return {"id": "123"} if method == "POST" else saved.copy()

    def patch(method, url, **kwargs):
        patches.append(kwargs["json"])
        saved.update(kwargs["json"])

    client.http = SimpleNamespace(json=get_or_create, request=patch)
    assert client.create_draft(article, _account_checked=True) == "123"
    assert any(p.get("titleImage") == uploaded(article.cover) for p in patches)
    assert saved["titleImage"] == uploaded(article.cover)
    assert uploaded(article.cover) not in saved["content"]
    _in_order(saved["content"], "开头。", "one.jpg", "中间。", "two.jpg", "结尾。")


def test_zhihu_verifies_cover_separately_from_body_images(tmp_path):
    article = _article(tmp_path)
    urls = [f"https://pic1.zhimg.com/{name}.jpg" for name in ("cover", "one", "two")]
    body = render(article, {str(path): url for path, url in zip(article.all_images(), urls)},
                  cover_first=False)
    client = ZhihuWeb.__new__(ZhihuWeb)
    client.http = SimpleNamespace(json=lambda *args, **kwargs: {
        "id": "123", "state": "published", "title": article.title,
        "titleImage": urls[0], "content": body})
    result = client.verify("123", expected=article,
                           evidence={"media": [client._image_key(url) for url in urls]})
    assert result.verification == "verified"


def test_zhihu_verifies_published_cover_moved_into_body(tmp_path):
    article = _article(tmp_path)
    urls = [f"https://pic1.zhimg.com/{name}.jpg" for name in ("cover", "one", "two")]
    body = render(article, {str(path): url for path, url in zip(article.all_images(), urls)},
                  cover_first=True)
    client = ZhihuWeb.__new__(ZhihuWeb)
    client.http = SimpleNamespace(json=lambda *args, **kwargs: {
        "id": "123", "state": "published", "title": article.title,
        "title_image": "", "content": body})
    result = client.verify("123", expected=article,
                           evidence={"media": [client._image_key(url) for url in urls]})
    assert result.verification == "verified"


def test_zhihu_rejects_wrong_published_cover_even_when_body_images_match(tmp_path):
    article = _article(tmp_path)
    urls = [f"https://pic1.zhimg.com/{name}.jpg" for name in ("cover", "one", "two")]
    body = render(article, {str(path): url for path, url in zip(article.all_images(),
                  ["https://pic1.zhimg.com/wrong.jpg", *urls[1:]])}, cover_first=True)
    client = ZhihuWeb.__new__(ZhihuWeb)
    client.http = SimpleNamespace(json=lambda *args, **kwargs: {
        "id": "123", "state": "published", "title": article.title,
        "title_image": "", "content": body})
    result = client.verify("123", expected=article,
                           evidence={"media": [client._image_key(url) for url in urls]})
    assert result.verification == "mismatch"


def test_sohu_sends_brief_separately_and_preserves_body_order(tmp_path):
    article = _article(tmp_path)
    posted = {}

    class Response:
        def json(self):
            return {"success": True, "data": "42"}

    def request(method, url, **kwargs):
        posted.update(kwargs["json"])
        return Response()

    client = SohuWeb.__new__(SohuWeb)
    client.account_id = "123"
    client.account = lambda: {"id": "123"}
    client.upload_image = lambda path: f"https://cdn.example.com/{path.name}"
    client._write_headers = lambda: {}
    client.http = SimpleNamespace(request=request)
    result = client.draft(article)
    assert result.status == "draft"
    assert result.verification == "unavailable"
    assert posted["brief"] == article.summary
    assert posted["cover"] == "https://cdn.example.com/cover.jpg"
    assert posted["cover"] not in posted["content"]
    assert "一句摘要" not in posted["content"]
    assert "<!--" not in posted["content"]
    _in_order(posted["content"], "开头。", "one.jpg", "中间。", "two.jpg", "结尾。")


def test_sohu_public_submission_keeps_cover_out_of_body(tmp_path):
    article = _article(tmp_path)
    posted = {}

    class Response:
        def json(self):
            return {"code": 2000000, "data": 42}

    def request(method, url, **kwargs):
        posted.update(kwargs["json"])
        return Response()

    client = SohuWeb.__new__(SohuWeb)
    client.account_id = "123"
    client.account = lambda: {"id": "123"}
    client.upload_image = lambda path: f"https://cdn.example.com/{path.name}"
    client._write_headers = lambda: {}
    client.http = SimpleNamespace(request=request)
    assert client.publish(article).status == "pending"
    assert posted["cover"] == "https://cdn.example.com/cover.jpg"
    assert posted["cover"] not in posted["content"]
    _in_order(posted["content"], "开头。", "one.jpg", "中间。", "two.jpg", "结尾。")


def test_sohu_verifies_cover_field_and_body_images_separately(tmp_path):
    article = _article(tmp_path)
    image_map = _image_map(article)
    body = render(article, image_map, cover_first=False)
    client = SohuWeb.__new__(SohuWeb)
    client.account_id = "123"
    client.list_articles = lambda: [{"id": "42", "title": article.title,
                                     "status": "published", "url": "https://www.sohu.com/a/42_123"}]
    client._write_headers = lambda: {}
    client.http = SimpleNamespace(request=lambda *args, **kwargs: SimpleNamespace(
        json=lambda: {"code": 2000000, "data": {"news": {
            "title": article.title, "cover": image_map[str(article.cover)], "content": body}}}))
    result = client.verify("42", expected=article,
                           evidence={"media": list(image_map.values())})
    assert result.verification == "verified"


def test_toutiao_verifies_cover_field_and_body_images_separately(tmp_path):
    article = _article(tmp_path)
    uris = [f"pgc-image/{name}123" for name in ("cover", "one", "two")]
    urls = [f"https://p3.toutiaoimg.com/{uri}~tplv" for uri in uris]
    body = render(article, {str(path): url for path, url in zip(article.all_images(), urls)},
                  cover_first=False)
    client = ToutiaoWeb.__new__(ToutiaoWeb)
    client.account = lambda: None
    client.account_id = "999"
    client.find_article = lambda *args, **kwargs: {"item_id": "456", "status": 2}
    client.detail = lambda *args: {"pgc_id": "123", "media_id": "999",
                                  "title": article.title, "content": body,
                                  "pgc_feed_covers": [{"uri": uris[0]}]}
    result = client.verify("123", expected=article, evidence={"media": uris})
    assert result.verification == "verified"


def test_toutiao_verifies_published_cover_inserted_at_start_of_body(tmp_path):
    article = _article(tmp_path)
    uris = [f"pgc-image/{name}123" for name in ("cover", "one", "two")]
    urls = [f"https://p3.toutiaoimg.com/{uri}~tplv" for uri in uris]
    body = render(article, {str(path): url for path, url in zip(article.all_images(), urls)},
                  cover_first=True)
    client = ToutiaoWeb.__new__(ToutiaoWeb)
    client.account = lambda: None
    client.account_id = "999"
    client.find_article = lambda *args, **kwargs: {"item_id": "456", "status": 2}
    client.detail = lambda *args: {"pgc_id": "123", "media_id": "999",
                                  "title": article.title, "content": body,
                                  "pgc_feed_covers": [{"origin_uri": uris[0]}]}
    result = client.verify("123", expected=article, evidence={"media": uris})
    assert result.verification == "verified"


def test_channels_without_summary_field_discard_it(tmp_path):
    article = _article(tmp_path)
    image_map = _image_map(article)
    html = render(article, image_map, cover_first=False)
    assert "一句摘要" not in html  # 知乎、头条、搜狐正文共用此渲染器
    assert image_map[str(article.cover)] not in html
    _in_order(html, "开头。", "one.jpg", "中间。", "two.jpg", "结尾。")
    uploads = [{"file_id": str(i), "width": 1, "height": 1, "size": 1}
               for i in range(3)]
    xhs = image_payload(article, uploads)
    assert "一句摘要" not in xhs["common"]["desc"]
    assert "summary" not in xhs["common"]
    assert [i["file_id"] for i in xhs["image_info"]["images"]] == [
        "spectrum/0", "spectrum/1", "spectrum/2"]


def test_netease_editor_blocks_interleave_images_and_text(tmp_path):
    article = _article(tmp_path)
    blocks = body_content_blocks(article)
    assert [(kind, value if kind == "text" else value.name) for kind, value in blocks] == [
        ("text", "开头。"), ("image", "one.jpg"), ("text", "中间。"),
        ("image", "two.jpg"), ("text", "结尾。")]


def test_netease_always_uses_cover_as_first_body_image(tmp_path):
    (tmp_path / "cover.jpg").write_bytes(b"image")
    source = tmp_path / "article.md"
    source.write_text("# 标题\n<!-- cover: cover.jpg -->\n开头。\n\n结尾。", encoding="utf-8")
    images, blocks = _netease_editor_media(Article.load(source))
    assert [path.name for path in images] == ["cover.jpg"]
    assert [(kind, value.name if kind == "image" else value) for kind, value in blocks] == [
        ("image", "cover.jpg"), ("text", "开头。\n\n结尾。")]

    article = _article(tmp_path)
    images, blocks = _netease_editor_media(article)
    assert [path.name for path in images] == ["cover.jpg", "one.jpg", "two.jpg"]
    assert [(kind, value.name if kind == "image" else value) for kind, value in blocks] == [
        ("image", "cover.jpg"), ("text", "开头。"), ("image", "one.jpg"), ("text", "中间。"),
        ("image", "two.jpg"), ("text", "结尾。")]


def test_netease_http_draft_starts_body_with_cover(tmp_path):
    (tmp_path / "cover.jpg").write_bytes(b"image")
    source = tmp_path / "article.md"
    source.write_text("# 标题\n<!-- cover: cover.jpg -->\n正文。", encoding="utf-8")
    article = Article.load(source)
    posted = {}
    client = NeteaseWeb.__new__(NeteaseWeb)
    client.account = lambda: None
    client.wemedia_id = "123"
    client.upload_image = lambda path: {"url": f"https://cdn.example.com/{path.name}"}

    def request(method, url, **kwargs):
        posted.update(kwargs["data"])
        return {"code": 1, "data": "docId=L8DBTPLM0556PYDT&pkId=null"}

    client.http = SimpleNamespace(json=request)
    client._submit(article, operation="saveDraft")
    assert posted["cover"] == "https://cdn.example.com/cover.jpg"
    assert posted["content"].count(posted["cover"]) == 1
    assert posted["content"].index(posted["cover"]) < posted["content"].index("正文。")

    article = _article(tmp_path)
    posted.clear()
    client._submit(article, operation="saveDraft")
    assert posted["content"].count(posted["cover"]) == 1
    _in_order(posted["content"], "cover.jpg", "开头。", "one.jpg", "中间。", "two.jpg", "结尾。")


def test_netease_editor_executes_interleaved_blocks_in_source_order(tmp_path):
    article = _article(tmp_path)
    images, blocks = _netease_editor_media(article)
    publisher = NeteaseBrowserPublish(
        [], article.title, "<p>正文</p>", body_images=images, body_blocks=blocks)
    calls = []
    publisher._paste_text = lambda page, value: calls.append(("text", value))
    publisher._paste_image = lambda page, value: calls.append(("image", value.name))
    publisher._insert_body(object())
    assert calls == [
        ("image", "cover.jpg"), ("text", "开头。"), ("image", "one.jpg"), ("text", "中间。"),
        ("image", "two.jpg"), ("text", "结尾。")]


def test_netease_rejects_images_without_markdown_positions(tmp_path):
    image = tmp_path / "one.jpg"
    image.write_bytes(b"image")
    with pytest.raises(ValueError, match="body_blocks"):
        NeteaseBrowserPublish([], "标题", "<p>开头。结尾。</p>", body_images=[image])


def test_netease_requires_selected_cover_to_be_first_editor_image(tmp_path):
    article = _article(tmp_path)
    images, blocks = _netease_editor_media(article)
    with pytest.raises(ValueError, match="正文首图"):
        NeteaseBrowserPublish([], article.title, "", cover_path=article.cover,
                              body_images=images, body_blocks=blocks[1:])


def test_netease_rejects_editor_that_moves_images_to_end(tmp_path):
    article = _article(tmp_path)
    publisher = NeteaseBrowserPublish(
        [], article.title, "<p>正文</p>", body_blocks=body_content_blocks(article))
    page = SimpleNamespace(evaluate=lambda _: ["开头。中间。结尾。", "", ""])
    with pytest.raises(HTTPFailure, match="顺序") as exc:
        publisher._verify_body_order(page)
    assert exc.value.kind == "validation"


def test_netease_accepts_editor_with_markdown_image_order(tmp_path):
    article = _article(tmp_path)
    publisher = NeteaseBrowserPublish(
        [], article.title, "<p>正文</p>", body_blocks=body_content_blocks(article))
    page = SimpleNamespace(evaluate=lambda _: ["开头。", "中间。", "结尾。"])
    publisher._verify_body_order(page)


def test_netease_ignores_image_caption_placeholder_in_real_editor_dom():
    """The editor's uneditable image widget includes UI text after each image."""
    playwright = pytest.importorskip("playwright.sync_api")
    publisher = NeteaseBrowserPublish([], "标题", "", body_blocks=[
        ("text", "开头。"), ("image", Path("one.jpg")),
        ("text", "中间。"), ("image", Path("two.jpg")),
        ("text", "结尾。"),
    ])
    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('''
                <div contenteditable="true"><div data-contents="true">
                  <div data-block="true"><span data-text="true">开头。</span></div>
                  <div data-block="true" contenteditable="false">
                    <div class="rich-editor-image"><img src="one.jpg"></div>
                    <figcaption>点击输入图片描述（最多30字）</figcaption>
                  </div>
                  <div data-block="true"><span data-text="true">中间。</span></div>
                  <div data-block="true" contenteditable="false">
                    <div class="rich-editor-image"><img src="two.jpg"></div>
                    <figcaption>点击输入图片描述（最多30字）</figcaption>
                  </div>
                  <div data-block="true"><span data-text="true">结尾。</span></div>
                </div></div>
            ''')
            publisher._verify_body_order(page)
        finally:
            browser.close()


def test_netease_order_mismatch_is_failed_before_submit(tmp_path, monkeypatch):
    article = _article(tmp_path)
    store = StorageLayout(tmp_path)
    cred_path = store.credentials("netease")
    cred_path.parent.mkdir(parents=True, exist_ok=True)
    cred_path.write_text(json.dumps({"cookies": [{"name": "session", "value": "test"}]}))

    def fail_before_submit(self, **kwargs):
        raise HTTPFailure("图文顺序与 Markdown 不一致", kind="validation")

    monkeypatch.setattr(NeteaseBrowserPublish, "run", fail_before_submit)
    result = _netease_browser_publish(article, store)
    assert result.status == "failed"


def test_netease_first_click_success_does_not_click_publish_again():
    publisher = NeteaseBrowserPublish([], "标题", "")

    class Response:
        url = "https://mp.163.com/wemedia/article/status/api/publishV2.do"
        status = 200
        request = SimpleNamespace(post_data="operation=publish")

        def text(self):
            return '{"code":1,"data":"docId=L8DBTPLM0556PYDT&pkId=null"}'

    class Page:
        def __init__(self):
            self.callback = None
            self.clicks = 0

        def on(self, event, callback):
            assert event == "response"
            self.callback = callback

        def click(self, *_args, **_kwargs):
            self.clicks += 1
            self.callback(Response())

        def wait_for_timeout(self, *_args):
            pass

    page = Page()
    publisher._quota_hit = lambda _: False
    assert publisher._submit_editor(page) is False
    assert page.clicks == 1
    assert publisher.submitted_id == "L8DBTPLM0556PYDT"


def test_netease_ambiguous_browser_result_reads_back_new_publication(tmp_path, monkeypatch):
    publisher = NeteaseBrowserPublish([], "标题", "", dest=tmp_path / "auth.json")
    snapshots = iter(({"old-id"}, {"old-id", "L8DBTPLM0556PYDT"}))
    publisher._published_ids = lambda: next(snapshots)

    class Loginer:
        def __init__(self, **_kwargs):
            pass

        def login(self, *_args, **_kwargs):
            publisher._submission_attempted = True
            return LoginResult(status="need_human", message="登录需要人工验证")

    monkeypatch.setattr("mulpubcli.browser.PlaywrightLoginer", Loginer)
    result = publisher.run()
    assert result["status"] == "published"
    assert result["remote_id"] == "L8DBTPLM0556PYDT"


def test_netease_ambiguous_browser_result_without_new_article_is_pending(tmp_path, monkeypatch):
    publisher = NeteaseBrowserPublish([], "标题", "", dest=tmp_path / "auth.json")
    snapshots = iter(({"old-id"}, {"old-id"}))
    publisher._published_ids = lambda: next(snapshots)

    class Loginer:
        def __init__(self, **_kwargs):
            pass

        def login(self, *_args, **_kwargs):
            publisher._submission_attempted = True
            return LoginResult(status="need_human", message="登录需要人工验证")

    monkeypatch.setattr("mulpubcli.browser.PlaywrightLoginer", Loginer)
    result = publisher.run()
    assert result["status"] == "pending"
    assert "核验" in result["message"]


def test_netease_navigation_error_after_submit_can_be_read_back(tmp_path, monkeypatch):
    publisher = NeteaseBrowserPublish([], "标题", "", dest=tmp_path / "auth.json")
    snapshots = iter((set(), {"L8DBTPLM0556PYDT"}))
    publisher._published_ids = lambda: next(snapshots)

    class Loginer:
        def __init__(self, **_kwargs):
            pass

        def login(self, *_args, **_kwargs):
            publisher._submission_attempted = True
            raise RuntimeError("page navigated during click")

    monkeypatch.setattr("mulpubcli.browser.PlaywrightLoginer", Loginer)
    result = publisher.run()
    assert result["status"] == "published"
    assert result["remote_id"] == "L8DBTPLM0556PYDT"


def test_netease_pending_browser_result_stays_pending_in_cli(tmp_path, monkeypatch):
    article = _article(tmp_path)
    store = StorageLayout(tmp_path)
    credential = store.credentials("netease")
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text(json.dumps({"cookies": [{"name": "session", "value": "test"}]}))
    monkeypatch.setattr(NeteaseBrowserPublish, "run", lambda *a, **k: {
        "status": "pending", "message": "网易提交结果未确认，请核验已发布列表；不要重发"})
    result = _netease_browser_publish(article, store)
    assert result.status == "pending"
    assert "登录" not in result.message


def test_netease_readback_publication_returns_published_in_cli(tmp_path, monkeypatch):
    article = _article(tmp_path)
    store = StorageLayout(tmp_path)
    credential = store.credentials("netease")
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text(json.dumps({"cookies": [{"name": "session", "value": "test"}]}))
    monkeypatch.setattr(NeteaseBrowserPublish, "run", lambda *a, **k: {
        "status": "published", "remote_id": "L8DBTPLM0556PYDT",
        "message": "已从网易列表确认发布"})
    result = _netease_browser_publish(article, store)
    assert result.status == "published"
    assert result.url == "https://www.163.com/dy/article/L8DBTPLM0556PYDT.html"


def test_netease_editor_keeps_paragraph_breaks_between_images(tmp_path):
    image = tmp_path / "one.jpg"
    image.write_bytes(b"image")
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"image")
    source = tmp_path / "article.md"
    source.write_text(
        "# 标题\n<!-- cover: cover.jpg -->\n\n"
        "第一段。\n\n第二段。\n\n![图](one.jpg)\n\n"
        "第三段。\n\n第四段。",
        encoding="utf-8",
    )
    blocks = body_content_blocks(Article.load(source))
    assert blocks == [
        ("text", "第一段。\n\n第二段。"), ("image", image),
        ("text", "第三段。\n\n第四段。")]


def test_netease_does_not_continue_when_editor_drops_an_image(tmp_path):
    image = tmp_path / "one.jpg"
    image.write_bytes(b"image")
    publisher = NeteaseBrowserPublish([], "标题", "<p>正文</p>")
    page = SimpleNamespace(evaluate=lambda *args: 0, wait_for_timeout=lambda *_: None)
    with pytest.raises(HTTPFailure, match="图片"):
        publisher._paste_image(page, image)


def test_netease_requires_a_new_image_when_editor_has_an_old_one(tmp_path):
    image = tmp_path / "one.jpg"
    image.write_bytes(b"image")
    publisher = NeteaseBrowserPublish([], "标题", "<p>正文</p>")

    def evaluate(script, payload=None):
        return 1 if "querySelectorAll('img').length" in script else True

    page = SimpleNamespace(evaluate=evaluate, wait_for_timeout=lambda *_: None)
    with pytest.raises(HTTPFailure, match="图片"):
        publisher._paste_image(page, image)


def test_netease_pastes_png_with_its_real_media_type(tmp_path):
    image = tmp_path / "one.png"
    image.write_bytes(b"png-data")
    sent = {}
    counts = iter((0, 1))

    def evaluate(script, payload=None):
        if payload is not None:
            sent.update(payload)
        return next(counts) if "querySelectorAll('img').length" in script else True

    publisher = NeteaseBrowserPublish([], "标题", "<p>正文</p>")
    page = SimpleNamespace(evaluate=evaluate, wait_for_timeout=lambda *_: None)
    publisher._paste_image(page, image)
    assert sent["mime_type"] == "image/png"


@pytest.mark.parametrize("platform", ["xiaohongshu", "netease"])
def test_gallery_or_browser_publish_rejects_remote_images_before_submitting(
        tmp_path, capsys, platform):
    (tmp_path / "cover.jpg").write_bytes(b"image")
    source = tmp_path / "article.md"
    source.write_text(
        "# 标题\n<!-- cover: cover.jpg -->\n\n"
        "开头。\n\n![远程图](https://example.com/pic.jpg)\n\n结尾。",
        encoding="utf-8",
    )
    args = SimpleNamespace(platform=platform, article=str(source), force=False, proxy=None)
    code = _cmd_publish(args, StorageLayout(tmp_path))
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["status"] == "failed"
    assert "远程图片" in payload["message"]
