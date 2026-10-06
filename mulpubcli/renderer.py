"""mulpubcli.renderer — Markdown → 平台 HTML 渲染器。

使用方式：

    from mulpubcli.renderer import render

    # 第一步：扫描 article，得到需要上传的本地图片列表（Article.body_images）
    # 第二步：平台 client 逐一上传图片，建立映射表
    image_map = {
        str(article.cover):          "https://cdn.platform.com/cover_uri",
        str(article.body_images[0]): "https://cdn.platform.com/body_img1_uri",
    }
    # 第三步：调用 render() 得到最终 HTML
    html = render(article, image_map)

render() 规则：
  - 封面图：文章渠道通过各自的封面字段单独提交；默认不插入正文。
    只有显式指定 cover_first=True 时，封面才以 <figure> 插在正文最前。
  - 正文段落：逐行解析 Markdown，段落间双换行分隔。
  - 正文内嵌图片：![alt](本地路径) → <figure><img src="平台URL"></figure>；
    未在 image_map 中的本地路径直接跳过（保证不生成破链）。
  - 远程图片（http/https）：原样输出，不需要上传。
"""
from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from .core import Article
from .http import HTTPFailure

_MD_IMG_RE = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')
_INLINE_RE = re.compile(
    r'!\[([^\]]*)\]\(([^)]+)\)'
    r'|\[([^\]]+)\]\((https?://[^)]+)\)'
    r'|`([^`]+)`|\*\*(.+?)\*\*|~~(.+?)~~|\*([^*\n]+)\*'
)
_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)$')
_LIST_RE = re.compile(r'^\s*(?:([-*+])\s+|(\d+)\.\s+)(.+)$')


class _ArticleHTML(HTMLParser):
    """Parse article HTML to compare visible text and image srcs across platforms.

    同时供知乎与头条回读核验使用，容忍编辑器额外加的属性。text 折叠空白，
    images 按文档顺序保留图片源地址。
    """
    BLOCKS = {'p', 'div', 'figure', 'br', 'li', 'h1', 'h2', 'h3', 'blockquote'}

    def __init__(self, content):
        super().__init__(convert_charrefs=True)
        self.parts, self.images = [], []
        if not isinstance(content, str):
            raise HTTPFailure('正文响应结构无效', kind='invalid_response')
        self.feed(content)
        self.close()

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCKS:
            self.parts.append(' ')
        if tag == 'img':
            source = dict(attrs).get('src') or ''
            if source.startswith('//'):
                source = 'https:' + source
            self.images.append(urlsplit(source)._replace(query='', fragment='').geturl())

    def handle_endtag(self, tag):
        if tag in self.BLOCKS:
            self.parts.append(' ')

    @property
    def text(self):
        return ' '.join(''.join(self.parts).split())


def _is_remote(src: str) -> bool:
    return src.startswith(("http://", "https://", "data:"))


def _local_path(src: str, base_dir: Path | None = None) -> Path | None:
    """Resolve a Markdown image src to a resolved local Path, or None if remote/missing.

    与 _render_body 的落图规则保持一致：本地路径相对稿件目录解析，
    远程 URL 不返回本地路径。
    """
    src = src.strip()
    if _is_remote(src):
        return None
    p = Path(src) if Path(src).is_absolute() else (base_dir or Path('.')) / src
    return p.resolve()


def body_content_blocks(article: Article) -> list[tuple[str, object]]:
    """把正文按文档原始顺序拆成可交替插入的内容块。

    每块是 ``('text', str)`` 或 ``('image', Path)``，顺序与 Markdown 正文一致，
    镜像 ``_render_body`` 的段落/图块划分，但保留本地图片路径而非丢弃。

    用途：网易发布用真实编辑器里「粘贴」逐块插入时，必须按原顺序贴文字、插图，
    否则图片会被全部追加到正文末尾。
    """
    body = article.body
    if not isinstance(body, str) or not body.strip():
        return []
    base_dir = article.source_dir
    blocks: list[tuple[str, object]] = []
    pending: list[str] = []

    def flush_text() -> None:
        if pending:
            text = '\n'.join(pending).strip('\n')
            if text:
                blocks.append(('text', text))
            pending.clear()

    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            if pending:
                pending.append('')
            continue
        sole = _MD_IMG_RE.fullmatch(stripped)
        if sole is not None:
            # 独立图片行 → 图块
            flush_text()
            p = _local_path(sole.group(2), base_dir)
            if p is not None:
                blocks.append(('image', p))
            continue
        # 文本行，可能内嵌图片 → 按出现顺序拆成文字/图块
        last = 0
        for m in _MD_IMG_RE.finditer(stripped):
            before = stripped[last:m.start()].strip()
            if before:
                pending.append(before)
            p = _local_path(m.group(2), base_dir)
            if p is not None:
                flush_text()
                blocks.append(('image', p))
            last = m.end()
        tail = stripped[last:].strip()
        if tail:
            pending.append(tail)
    flush_text()
    return blocks


def _resolve_src(src: str, image_map: dict[str, str], *, base_dir: Path | None = None) -> str | None:
    """Return the platform URL for a given image src, or None if unavailable.

    src may be written relative to the Markdown file's directory (the common
    case: ``![图](images/a.png)``). Resolve against base_dir (article.source_dir)
    so the key matches Article.body_images regardless of the process CWD —
    resolving against CWD silently drops every body image when the CLI runs
    from a different directory.
    """
    src = src.strip()
    if _is_remote(src):
        return src
    # Try direct key match first (CWD-independent literal, e.g. absolute paths)
    if src in image_map:
        return image_map[src]
    # Resolve relative to the Markdown's directory, then fall back to CWD.
    if src and base_dir is not None and not Path(src).is_absolute():
        resolved = str((base_dir / src).resolve())
        if resolved in image_map:
            return image_map[resolved]
    resolved = str(Path(src).resolve())
    return image_map.get(resolved)


def _render_body(body: str, image_map: dict[str, str], *, base_dir: Path | None = None) -> str:
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
        """Render the supported inline Markdown without accepting raw HTML."""
        result = []
        last = 0
        for m in _INLINE_RE.finditer(line):
            before = escape(line[last:m.start()])
            if before:
                result.append(before)
            if m.group(1) is not None:
                alt, src = m.group(1), m.group(2)
                url = _resolve_src(src, image_map, base_dir=base_dir)
                if url:
                    result.append(f'<img src="{escape(url, quote=True)}" alt="{escape(alt)}">')
            elif m.group(3) is not None:
                label, url = m.group(3), m.group(4)
                result.append(f'<a href="{escape(url, quote=True)}">{_render_inline(label)}</a>')
            elif m.group(5) is not None:
                result.append(f'<code>{escape(m.group(5))}</code>')
            elif m.group(6) is not None:
                result.append(f'<strong>{_render_inline(m.group(6))}</strong>')
            elif m.group(7) is not None:
                result.append(f'<del>{_render_inline(m.group(7))}</del>')
            else:
                result.append(f'<em>{_render_inline(m.group(8))}</em>')
            last = m.end()
        tail = escape(line[last:])
        if tail:
            result.append(tail)
        return "".join(result)

    lines = body.splitlines()
    i = 0
    while i < len(lines):
        raw_line = lines[i]
        stripped = raw_line.strip()
        i += 1

        # Blank line → end current paragraph
        if not stripped:
            _flush_paragraph()
            continue

        heading = _HEADING_RE.fullmatch(stripped)
        if heading:
            _flush_paragraph()
            level = len(heading.group(1))
            parts.append(f'<h{level}>{_render_inline(heading.group(2))}</h{level}>')
            continue

        if stripped.startswith('> '):
            _flush_paragraph()
            quoted = [stripped[2:]]
            while i < len(lines) and lines[i].strip().startswith('> '):
                quoted.append(lines[i].strip()[2:])
                i += 1
            parts.append('<blockquote><p>' + '<br>'.join(_render_inline(s) for s in quoted) + '</p></blockquote>')
            continue

        item = _LIST_RE.fullmatch(stripped)
        if item:
            _flush_paragraph()
            ordered = item.group(2) is not None
            tag = 'ol' if ordered else 'ul'
            items = [_render_inline(item.group(3))]
            while i < len(lines):
                next_item = _LIST_RE.fullmatch(lines[i].strip())
                if next_item is None or (next_item.group(2) is not None) != ordered:
                    break
                items.append(_render_inline(next_item.group(3)))
                i += 1
            parts.append(f'<{tag}>' + ''.join(f'<li>{s}</li>' for s in items) + f'</{tag}>')
            continue

        # Check if this line is a standalone image (nothing else on the line)
        sole = _MD_IMG_RE.fullmatch(stripped)
        if sole:
            _flush_paragraph()
            alt, src = sole.group(1), sole.group(2)
            url = _resolve_src(src, image_map, base_dir=base_dir)
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


def plain_markdown_text(body: str) -> str:
    """Flatten supported Markdown for publishing paths that accept plain text."""
    from .core import strip_markdown_images

    body = strip_markdown_images(body)
    out = []
    for line in body.splitlines():
        line = re.sub(r'^\s*#{1,6}\s+', '', line)
        line = re.sub(r'^\s*>\s?', '', line)
        line = re.sub(r'^\s*(?:[-*+]\s+|\d+\.\s+)', '', line)
        line = re.sub(r'\[([^\]]+)\]\(https?://[^)]+\)', r'\1', line)
        line = re.sub(r'\*\*(.+?)\*\*|~~(.+?)~~|\*([^*\n]+)\*|`([^`]+)`',
                      lambda m: next(s for s in m.groups() if s is not None), line)
        out.append(line.rstrip())
    return '\n'.join(out).strip()


def render(
    article: Article,
    image_map: dict[str, str],
    *,
    cover_first: bool = False,
    include_title: bool = False,
) -> str:
    """Render article to platform HTML.

    Args:
        article:       Article data model.
        image_map:     Mapping of local image path strings → platform CDN URLs.
                       Keys should be str(Path.resolve()) or the literal src from markdown.
        cover_first:   If True, insert the cover image as a <figure> before the body.
                       Defaults to False because article covers are submitted separately.
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

    body_html = _render_body(article.body, image_map, base_dir=article.source_dir)
    if body_html:
        parts.append(body_html)

    return "\n".join(parts)
