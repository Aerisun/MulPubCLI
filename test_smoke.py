"""Smoke test: Article.load + renderer, no network calls."""
import pathlib, tempfile, sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from mutipubcli.core import Article
from mutipubcli.renderer import render

with tempfile.TemporaryDirectory() as tmp:
    tmp = pathlib.Path(tmp)
    body_img = tmp / "photo.jpg"
    body_img.write_bytes(b"\xff\xd8\xff" + b"\x00" * 10)  # minimal fake JPEG header
    cover = tmp / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff" + b"\x00" * 10)
    md = tmp / "art.md"
    md.write_text(
        f"# 雾中独行\n\n清晨的光线很薄，像被水洗过一样。\n\n![配图]({body_img})\n\n到了某个路口，他停下来。",
        encoding="utf-8",
    )

    a = Article.load(md, cover)
    assert a.title == "雾中独行"
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
    md2.write_text("# 纯文字\n\n第一段\n\n第二段", encoding="utf-8")
    a2 = Article.load(md2, cover)
    assert len(a2.body_images) == 0
    html4 = render(a2, {str(cover): "https://cdn.example.com/cover.jpg"}, cover_first=True)
    assert "<p>第一段</p>" in html4
    assert "<p>第二段</p>" in html4
    print(f"✓ render(plain text): paragraphs correct, no stray image tags")

print("\nAll smoke tests PASSED ✓")
