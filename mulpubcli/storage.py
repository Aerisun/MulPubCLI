"""mulpubcli.storage — 内部文件存储规划，统一管理所有路径与临时文件。

目录约定（相对于项目根，即 pyproject.toml 所在目录）：

    .storage/
    ├── auth/               # 登录凭证，权限 600，永久保留
    │   ├── xiaohongshu.json
    │   ├── zhihu.json
    │   └── toutiao.json
    ├── qr/                 # 临时二维码 PNG，超过 10 分钟自动清理
    │   ├── xiaohongshu-login.png
    │   ├── zhihu-login.png
    │   └── toutiao-login.png
    ├── results/            # 发布结果账本，权限 600，永久保留
    │   └── <platform>-<sha256prefix>.json
    └── tmp/                # 其他临时文件（上传中间产物等），任务完成后清理

所有目录在第一次访问时自动创建，权限 700。
二维码文件超过 QR_MAX_AGE_SECONDS 后被主动清理（每次 login 命令触发）。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

# QR codes older than this are considered stale and are deleted
QR_MAX_AGE_SECONDS = 600  # 10 minutes


def _project_root() -> Path:
    """Locate the project root by walking up from this file until we find pyproject.toml or .storage."""
    here = Path(__file__).resolve().parent
    for candidate in [here.parent, here.parent.parent]:
        if (candidate / "pyproject.toml").exists() or (candidate / ".storage").exists():
            return candidate
    return here.parent  # fallback


class StorageLayout:
    """Single source of truth for all file paths used by mulpubcli."""

    def __init__(self, root: Path | None = None):
        self.root = root or _project_root()
        self._base = self.root / ".storage"

    # ------------------------------------------------------------------ dirs

    @property
    def auth_dir(self) -> Path:
        return self._ensure(self._base / "auth")

    @property
    def qr_dir(self) -> Path:
        return self._ensure(self._base / "qr")

    @property
    def results_dir(self) -> Path:
        return self._ensure(self._base / "results")

    @property
    def tmp_dir(self) -> Path:
        return self._ensure(self._base / "tmp")

    # ---------------------------------------------------------------- files

    def credentials(self, platform: str) -> Path:
        """Permanent credentials file for a platform. Stored at 600."""
        return self.auth_dir / f"{platform}.json"

    def qr_image(self, platform: str) -> Path:
        """Temporary QR code PNG for login."""
        return self.qr_dir / f"{platform}-login.png"

    def lock_file(self, platform: str) -> Path:
        """Per-platform advisory lock to prevent concurrent writes."""
        return self.auth_dir / f"{platform}.lock"

    def tmp_file(self, name: str) -> Path:
        """A named temporary file under .storage/tmp/."""
        return self.tmp_dir / name

    # ------------------------------------------------------------- cleanup

    def cleanup_stale_qr(self) -> list[Path]:
        """Delete QR images older than QR_MAX_AGE_SECONDS. Returns deleted paths."""
        deleted: list[Path] = []
        if not self.qr_dir.exists():
            return deleted
        now = time.time()
        for f in self.qr_dir.glob("*.png"):
            try:
                age = now - f.stat().st_mtime
                if age > QR_MAX_AGE_SECONDS:
                    f.unlink()
                    deleted.append(f)
            except OSError:
                pass
        return deleted

    def cleanup_tmp(self) -> None:
        """Remove everything under .storage/tmp/."""
        if not self.tmp_dir.exists():
            return
        for f in self.tmp_dir.iterdir():
            try:
                if f.is_file():
                    f.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------- helpers

    def _ensure(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def describe(self) -> dict:
        """Return a human-readable summary of current storage state."""
        def _size(p: Path) -> str:
            if not p.exists():
                return "（不存在）"
            files = list(p.iterdir()) if p.is_dir() else []
            return f"{len(files)} 个文件"

        return {
            "root": str(self.root),
            "auth":    _size(self._base / "auth"),
            "qr":      _size(self._base / "qr"),
            "results": _size(self._base / "results"),
            "tmp":     _size(self._base / "tmp"),
        }


# Module-level default instance (can be overridden in tests)
default_storage = StorageLayout()
