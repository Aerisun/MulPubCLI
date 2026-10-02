"""mutipubcli.renderer — Markdown → 平台 HTML 渲染器。

使用方式：

    from mutipubcli.renderer import render

    # 第一步：扫描 article，得到需要上传的本地图片列表（Article.body_images）
    # 第二步：平台 client 逐一上传图片，建立映射表
    image_map = {
        str(article.cover):          "https://cdn.platform.com/cover_uri",
        str(article.body_images[0]): "https://cdn.platform.com/body_img1_uri",
    }
    # 第三步：调用 render() 得到最终 HTML
    html = render(article, image_map, cover_first=True)

render() 规则：
  - 封面图：由 image_map[str(article.cover)] 决定 URL；
    若 cover_first=True，封面以 <figure> 插在正文最前；
    知乎需要 cover_first=False（知乎 API 单独处理封面字段）。
  - 正文段落：逐行解析 Markdown，段落间双换行分隔。
  - 正文内嵌图片：![alt](本地路径) → <figure><img src="平台URL"></figure>；
    未在 image_map 中的本地路径直接跳过（保证不生成破链）。
  - 远程图片（http/https）：原样输出，不需要上传。
"""
from __future__ import annotations

import re
from html import escape
from pathlib import Path

from .core import Article

_MD_IMG_RE = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')


def _is_remote(src: str) -> bool:
    return src.startswith(("http://", "https://", "data:"))


def _resolve_src(src: str, image_map: dict[str, str]) -> str | None:
    """Return the platform URL for a given image src, or None if unavailable."""
    src = src.strip()
    if _is_remote(src):
        return src
    # Try direct key match first, then resolved absolute path
    if src in image_map:
        return image_map[src]
    resolved = str(Path(src).resolve())
    return image_map.get(resolved)


def _render_body(body: str, image_map: dict[str, str]) -> str:
    """Convert Markdown body to HTML.

    Layout rules:
      - Blank line → paragraph break
      - Line that is solely an image tag → standalone <figure><img></figure>
      - Line with inline image(s) → escaped text with <img> tags inline
      - All other lines → <p> with <br> between consecutive non-blank lines
    """
    parts: list[str] = []
    paragraph: list[str] = []

    def _flush_paragraph() -> None:
        if paragraph:
            parts.append(f'<p>{"<br>".join(paragraph)}</p>')
            paragraph.clear()

    def _render_inline(line: str) -> str:
        """Escape text and substitute image tags within a single line."""
        result = []
        last = 0
        for m in _MD_IMG_RE.finditer(line):
            # Escape text before this image
            before = escape(line[last:m.start()])
            if before:
                result.append(before)
            alt, src = m.group(1), m.group(2)
            url = _resolve_src(src, image_map)
            if url:
                result.append(f'<img src="{escape(url, quote=True)}" alt="{escape(alt)}">')
            # If no URL for local image, drop it silently
            last = m.end()
        # Remaining text after last image
        tail = escape(line[last:])
        if tail:
            result.append(tail)
        return "".join(result)

    for raw_line in body.splitlines():
        stripped = raw_line.strip()

        # Blank line → end current paragraph
        if not stripped:
            _flush_paragraph()
            continue

        # Check if this line is a standalone image (nothing else on the line)
        sole = _MD_IMG_RE.fullmatch(stripped)
        if sole:
            _flush_paragraph()
            alt, src = sole.group(1), sole.group(2)
            url = _resolve_src(src, image_map)
            if url:
                parts.append(
                    f'<figure><img src="{escape(url, quote=True)}" alt="{escape(alt)}"></figure>'
                )
            # local image without URL → silently omit
            continue

        # Normal text line (may contain inline images)
        paragraph.append(_render_inline(stripped))

    _flush_paragraph()
    return "\n".join(parts)


def render(
    article: Article,
    image_map: dict[str, str],
    *,
    cover_first: bool = True,
    include_title: bool = False,
) -> str:
    """Render article to platform HTML.

    Args:
        article:       Article data model.
        image_map:     Mapping of local image path strings → platform CDN URLs.
                       Keys should be str(Path.resolve()) or the literal src from markdown.
        cover_first:   If True, insert the cover image as a <figure> before the body.
                       Set False when the platform API handles the cover separately.
        include_title: If True, prepend <h1>title</h1>. Most platforms supply the title
                       through a dedicated API field, so this defaults to False.

    Returns:
        HTML string ready to submit to the platform API.
    """
    parts: list[str] = []

    if include_title:
        parts.append(f"<h1>{escape(article.title)}</h1>")

    if cover_first:
        cover_url = image_map.get(str(article.cover))
        if cover_url:
            parts.append(
                f'<figure><img src="{escape(cover_url, quote=True)}" alt="文章配图"></figure>'
            )

    body_html = _render_body(article.body, image_map)
    if body_html:
        parts.append(body_html)

    return "\n".join(parts)
