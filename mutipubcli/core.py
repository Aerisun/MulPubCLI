"""mutipubcli.core — 共享数据模型。

Article
    title       : str          — 文章标题（从 Markdown 第一行 # 提取）
    body        : str          — 正文 Markdown 原文（不含标题行）
    cover       : Path         — 封面图片本地路径（必须存在）
    body_images : tuple[Path]  — 正文中引用的本地图片路径列表（自动提取，可为空）

PublishResult
    status       : str         — published / draft / pending / failed / skipped
    message      : str
    url          : str | None  — 平台公开链接（已发表时有值）
    platform     : str | None
    verification : str | None  — verified / mismatch / missing_evidence / unavailable
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# Regex to detect Markdown image syntax: ![alt](src)
_MD_IMG_RE = re.compile(r'!\[[^\]]*\]\(([^)]+)\)')


def _is_local(src: str) -> bool:
    return not src.startswith(('http://', 'https://', 'data:'))


def _extract_body_images(body: str, base_dir: Path) -> tuple[Path, ...]:
    """Return absolute paths of all local images referenced in the Markdown body."""
    seen: dict[str, Path] = {}
    for src in _MD_IMG_RE.findall(body):
        src = src.strip()
        if not _is_local(src):
            continue
        p = Path(src) if Path(src).is_absolute() else (base_dir / src)
        p = p.resolve()
        # De-duplicate by resolved path, preserve order
        if str(p) not in seen:
            seen[str(p)] = p
    return tuple(seen.values())


def strip_markdown_images(text: str) -> str:
    """Remove Markdown image syntax ``![alt](src)`` from plain text.

    面向把图片单独放图集、正文只存纯文本的平台（如小红书），正文不应回显
    Markdown 图片语法。仅删除图片标记，保留周围正文与换行结构。
    """
    return _MD_IMG_RE.sub('', text)


@dataclass(frozen=True)
class Article:
    title: str
    body: str                           # Raw Markdown (no title line)
    cover: Path                         # Cover image, absolute
    body_images: tuple[Path, ...] = field(default_factory=tuple)  # In-body local images

    @classmethod
    def load(cls, source: Path, cover: Path) -> Article:
        source = source.resolve()
        cover  = cover.resolve()

        if not source.is_file():
            raise ValueError(f"稿件不存在：{source}")
        if not cover.is_file():
            raise ValueError(f"封面图不存在：{cover}")

        lines = source.read_text(encoding="utf-8").strip().splitlines()
        if not lines or not lines[0].startswith("# "):
            raise ValueError("稿件第一行须为 Markdown 一级标题（# 标题）")

        title = lines[0][2:].strip()
        body  = "\n".join(lines[1:]).strip()

        if not title:
            raise ValueError("标题不能为空")
        if not body:
            raise ValueError("正文不能为空")

        body_images = _extract_body_images(body, base_dir=source.parent)
        # Validate that all referenced local images actually exist
        missing = [str(p) for p in body_images if not p.is_file()]
        if missing:
            raise ValueError(f"正文中以下本地图片不存在：{', '.join(missing)}")

        return cls(title=title, body=body, cover=cover, body_images=body_images)

    def all_images(self) -> tuple[Path, ...]:
        """Cover first, then body images in order — all unique local image paths."""
        seen: dict[str, Path] = {str(self.cover): self.cover}
        for p in self.body_images:
            if str(p) not in seen:
                seen[str(p)] = p
        return tuple(seen.values())

    def describe(self) -> dict:
        return {
            "title": self.title,
            "body_length": len(self.body),
            "cover": str(self.cover),
            "body_images_count": len(self.body_images),
            "body_images": [str(p) for p in self.body_images],
        }


@dataclass(frozen=True)
class PublishResult:
    status:       str
    message:      str
    url:          str | None = None
    platform:     str | None = None
    verification: str | None = None


# ─────────────────────────────────────────────
# Content fingerprinting (for idempotent ledger)
# ─────────────────────────────────────────────

def content_fingerprint(platform: str, title: str, body: str) -> dict:
    """Stable hash of (platform, title, normalised body) for dedup."""
    if platform == "xiaohongshu":
        body = "\n".join(line.strip() for line in body.splitlines() if line.strip())
    else:
        body = " ".join(body.split())
    raw = json.dumps([1, platform, title, body], ensure_ascii=False).encode("utf-8")
    return {"version": 1, "sha256": hashlib.sha256(raw).hexdigest()}


def content_matches(platform: str, title: str, body: str, evidence: dict) -> bool:
    actual = content_fingerprint(platform, title, body)
    return all(evidence.get(k) == v for k, v in actual.items())


# ─────────────────────────────────────────────
# URL classification helpers
# ─────────────────────────────────────────────

def classify_public_url(platform: str, url: str | None) -> PublishResult:
    from urllib.parse import urlparse
    if not url:
        return PublishResult("pending", "尚无可核验的公开链接", platform=platform)
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path
    valid = (
        platform == "wechat_mp"
        and host == "mp.weixin.qq.com"
        and ((path.startswith("/s/") and len(path) > 3) or (path == "/s" and bool(parsed.query)))
    )
    if parsed.scheme == "https" and valid:
        return PublishResult("published", "已取得平台公开文章链接", url=url, platform=platform)
    return PublishResult("pending", "链接不是该平台的公开文章地址", platform=platform)
