"""Smoke test: Article.load + renderer, no network calls."""
import pathlib, tempfile, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mulpubcli.core import Article
from mulpubcli.renderer import render, body_content_blocks
from mulpubcli.platforms.netease import client as _netease_client

# ── NetEase contentState 映射（真实发布记录 contentState=3=已发布；受限由 unrecomReason 表达）──
_NECASES = [
    ({'contentState': 3}, 'published'),
    ({'contentState': 3, 'unrecomReason': '该内容分发受限'}, 'published(分发受限)'),
    ({'contentState': 3, 'unrecomReason': '0'}, 'published'),
    ({'contentState': 0}, 'draft'),
    ({'contentState': 1}, 'pending'),
    ({'contentState': 2}, 'unknown'),
]
for _item, _exp in _NECASES:
    _got = _netease_client._content_state(_item)
    assert _got == _exp, f"contentState mapping {_item!r}: got {_got!r}, expected {_exp!r}"
print(f"✓ netease contentState mapping: {len(_NECASES)} cases OK")

# ── 网易正文内容块顺序：图片必须嵌在文中对应位置，而非全部沉底 ──
_tmpdir = tempfile.mkdtemp()
_tb = pathlib.Path(_tmpdir)
_cover = _tb / "cover.jpg"; _cover.write_bytes(b"\xff\xd8\xff")
_im1 = _tb / "im1.jpg"; _im1.write_bytes(b"\xff\xd8\xff")
_im2 = _tb / "im2.jpg"; _im2.write_bytes(b"\xff\xd8\xff")

def _blocks_of(md_body: str):
    p = _tb / "blocks.md"
    p.write_text(f"# 标题\n\n<!-- cover: cover.jpg -->\n\n{md_body}", encoding="utf-8")
    return body_content_blocks(Article.load(p))

_blks = _blocks_of("首段\n\n![首图](im1.jpg)\n\n中段\n\n![尾图](im2.jpg)\n\n末段")
_seq = [k for k, _v in _blks]
_img_pos = [i for i, k in enumerate(_seq) if k == "image"]
# 两张图都应插在文字之间（不挨在一起沉底），且首图不在全文最前
assert _seq[0] == "text", "正文应以文字开头"
assert len(_img_pos) == 2, f"应有 2 张图，实际位置 {_seq}"
assert _img_pos[1] - _img_pos[0] > 1, "两张图被文字分隔，不应连续沉底"
print(f"✓ netease body content blocks interleaved: seq={_seq}")

with tempfile.TemporaryDirectory() as tmp:
    tmp = pathlib.Path(tmp)
    body_img = tmp / "photo.jpg"
    body_img.write_bytes(b"\xff\xd8\xff" + b"\x00" * 10)  # minimal fake JPEG header
    cover = tmp / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff" + b"\x00" * 10)
    md = tmp / "art.md"
    md.write_text(
        f"# 雾中独行\n\n<!-- cover: cover.jpg -->\n\n<!-- summary: 在浓雾与记忆之间，一个人独自走向路口 -->\n\n清晨的光线很薄，像被水洗过一样。\n\n![配图]({body_img})\n\n到了某个路口，他停下来。",
        encoding="utf-8",
    )

    a = Article.load(md)
    assert a.title == "雾中独行"
    assert a.summary == "在浓雾与记忆之间，一个人独自走向路口"
    assert len(a.body_images) == 1
    assert a.body_images[0].resolve() == body_img.resolve()
    assert a.cover.resolve() == cover.resolve()
    print(f"✓ Article.load: title={a.title!r}, body_images={len(a.body_images)}")

    image_map = {
        str(cover):    "https://cdn.example.com/cover.jpg",
        str(body_img): "https://cdn.example.com/photo.jpg",
    }

    # Test: cover_first=True (Zhihu style)
    html = render(a, image_map, cover_first=True, include_title=False)
    assert "cover.jpg" in html, "cover missing from HTML"
    assert "photo.jpg" in html, "body image missing from HTML"
    assert "<figure>" in html
    assert "<p>" in html
    assert html.index("cover.jpg") < html.index("photo.jpg"), "cover should come before body image"
    print(f"✓ render(cover_first=True): cover before body image, both present")

    # Test: cover_first=False (Toutiao style — cover is a separate div, body renderer skips it)
    html2 = render(a, image_map, cover_first=False, include_title=False)
    assert "cover.jpg" not in html2, "cover should not appear in body when cover_first=False"
    assert "photo.jpg" in html2, "body image must still be present"
    print(f"✓ render(cover_first=False): cover excluded, body image present")

    # Test: include_title
    html3 = render(a, image_map, cover_first=False, include_title=True)
    assert "<h1>雾中独行</h1>" in html3
    print(f"✓ render(include_title=True): title present")

    # Test: body without images in text lines
    md2 = tmp / "plain.md"
    md2.write_text("# 纯文字\n\n<!-- cover: cover.jpg -->\n\n第一段\n\n第二段", encoding="utf-8")
    a2 = Article.load(md2)
    assert len(a2.body_images) == 0
    html4 = render(a2, {str(cover): "https://cdn.example.com/cover.jpg"}, cover_first=True)
    assert "<p>第一段</p>" in html4
    assert "<p>第二段</p>" in html4
    print(f"✓ render(plain text): paragraphs correct, no stray image tags")

print("\nAll smoke tests PASSED ✓")
