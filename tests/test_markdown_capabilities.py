"""Article metadata and formatting sent by each publishing path."""

import pytest

from mulpubcli.core import Article
from mulpubcli.__main__ import _build_parser
from mulpubcli.renderer import body_content_blocks, render, plain_markdown_text
from mulpubcli.platforms.xiaohongshu.client import XHSHTTP, image_payload
from mulpubcli.platforms.netease.browser_publish import NeteaseBrowserPublish


def article_file(tmp_path, body):
    (tmp_path / "cover.jpg").write_bytes(b"image")
    source = tmp_path / "article.md"
    source.write_text(
        "<!-- title: 整篇文章标题 -->\n\n<!-- cover: cover.jpg -->\n\n" + body,
        encoding="utf-8",
    )
    return Article.load(source)


def test_special_title_keeps_first_markdown_heading_in_body(tmp_path):
    article = article_file(tmp_path, "# 正文一级标题\n\n段落。")
    assert article.title == "整篇文章标题"
    assert article.body.startswith("# 正文一级标题")
    assert "title:" not in article.body


def test_second_title_directive_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="只能出现在稿件第一行"):
        article_file(tmp_path, "<!-- title: 另一个标题 -->\n正文。")


def test_hash_heading_cannot_be_used_as_article_title(tmp_path):
    (tmp_path / "cover.jpg").write_bytes(b"image")
    source = tmp_path / "article.md"
    source.write_text("# 正文标题\n<!-- cover: cover.jpg -->\n正文。", encoding="utf-8")
    with pytest.raises(ValueError, match="标题指令"):
        Article.load(source)


def test_publish_help_shows_special_title_syntax(capsys):
    with pytest.raises(SystemExit) as exc:
        _build_parser().parse_args(["publish", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "<!-- title: 标题 -->" in help_text
    assert "# 标题" not in help_text


def test_rich_html_contains_basic_markdown_formatting(tmp_path):
    article = article_file(tmp_path, "## 小节\n\n> 一段**引用**\n\n普通**加粗**与*斜体*。")
    html = render(article, {})
    assert "<h2>小节</h2>" in html
    assert "<blockquote>" in html
    assert "<strong>引用</strong>" in html
    assert "<strong>加粗</strong>" in html
    assert "<em>斜体</em>" in html
    assert "整篇文章标题" not in html


def test_rich_html_handles_lists_links_code_and_escapes_raw_html(tmp_path):
    article = article_file(tmp_path, "- 第一项\n- 第二项\n\n访问[站点](https://example.com)和`x < y`。<script>")
    html = render(article, {})
    assert "<ul><li>第一项</li><li>第二项</li></ul>" in html
    assert '<a href="https://example.com">站点</a>' in html
    assert "<code>x &lt; y</code>" in html
    assert "&lt;script&gt;" in html


def test_plain_paths_remove_markdown_markers_without_losing_words(tmp_path):
    article = article_file(tmp_path, "## 小节\n\n> 一段**引用**\n\n普通**加粗**与*斜体*。")
    plain = plain_markdown_text(article.body)
    assert plain == "小节\n\n一段引用\n\n普通加粗与斜体。"
    assert image_payload(article, [])["common"]["desc"] == plain
    assert body_content_blocks(article) == [("text", "## 小节\n\n> 一段**引用**\n\n普通**加粗**与*斜体*。")]


def test_plain_paths_keep_link_label_and_list_text(tmp_path):
    article = article_file(tmp_path, "1. **第一项**\n2. [第二项](https://example.com)")
    assert plain_markdown_text(article.body) == "第一项\n第二项"


def test_xiaohongshu_length_check_uses_submitted_plain_text(tmp_path):
    article = article_file(tmp_path, "**" + "字" * 1000 + "**")
    client = XHSHTTP.__new__(XHSHTTP)
    client.upload_image = lambda path: {"file_id": "cover", "width": 1, "height": 1, "size": 1}
    sent = []
    client.call = lambda *args, **kwargs: sent.append(args[2]) or {"data": {}}
    assert client.publish(article).status == "pending"
    assert sent[0]["common"]["desc"] == "字" * 1000


def test_netease_browser_pastes_plain_text_and_verifies_it():
    publisher = NeteaseBrowserPublish([], "标题", "", body_blocks=[("text", "## 小节\n\n> **引用**")])
    publisher._focus_editor = lambda page: None
    pasted = []

    class Page:
        def evaluate(self, script, value=None):
            if value is not None:
                pasted.append(value)
            else:
                return ["小节引用"]

        def wait_for_timeout(self, ms):
            pass

    page = Page()
    publisher._paste_text(page, publisher.body_blocks[0][1])
    publisher._verify_body_order(page)
    assert pasted == ["小节\n\n引用"]
