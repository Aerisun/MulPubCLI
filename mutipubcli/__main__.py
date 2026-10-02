"""mutipubcli — 统一多平台 HTTP 发布 CLI。

命令一览：
  mutipubcli login   <platform> [--poll] [--refresh] [--method sms] [--confirm]
  mutipubcli check   <platform>
  mutipubcli publish <platform> --article FILE --cover FILE
  mutipubcli draft   <platform> --article FILE --cover FILE
  mutipubcli verify  <platform> --id ARTICLE_ID [--article FILE]
  mutipubcli status  [--platform <platform>]
  mutipubcli storage            # 查看内部存储占用

支持平台: xiaohongshu  zhihu  toutiao
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .core import Article, PublishResult
from .http import HTTPFailure, session_lock
from .ledger import ResultLedger
from .storage import StorageLayout, default_storage

PLATFORMS = ("xiaohongshu", "zhihu", "toutiao")

# ─────────────────────────────────────────────
# Platform client factory
# ─────────────────────────────────────────────

def _load_client(platform: str, store: StorageLayout):
    """Load a platform client from saved credentials. Raises if credentials missing."""
    path = store.credentials(platform)
    if not path.is_file():
        raise FileNotFoundError(
            f"尚无 {platform} 凭证（{path}），请先执行: mutipubcli login {platform}"
        )
    if path.stat().st_mode & 0o077:
        raise PermissionError(f"凭证文件权限过宽，请修为 600：{path}")
    if platform == "zhihu":
        from .platforms.zhihu.client import ZhihuWeb
        return ZhihuWeb.load(path)
    if platform == "toutiao":
        from .platforms.toutiao.client import ToutiaoWeb
        return ToutiaoWeb.load(path)
    if platform == "xiaohongshu":
        from .platforms.xiaohongshu.client import XHSHTTP
        ref = store.root / ".storage" / "references" / "xhs-api"
        return XHSHTTP(path, source=ref)
    raise ValueError(f"不支持的平台: {platform}")


def _new_client(platform: str):
    """Create a fresh unauthenticated client for login flows."""
    if platform == "zhihu":
        from .platforms.zhihu.client import ZhihuWeb
        return ZhihuWeb()
    if platform == "toutiao":
        from .platforms.toutiao.client import ToutiaoWeb
        return ToutiaoWeb()
    raise ValueError(f"{platform} 暂不支持通过此方式初始化")


def _close(client) -> None:
    """Safely close any HTTP session held by the client."""
    if hasattr(client, "close"):
        try:
            client.close()
        except Exception:
            pass


# ─────────────────────────────────────────────
# Article / cover loader
# ─────────────────────────────────────────────

def _load_article(article_path: str, cover_path: str) -> Article:
    return Article.load(Path(article_path), Path(cover_path))


# ─────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────

def _out(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _out_result(result: PublishResult) -> None:
    _out(asdict(result))


# ─────────────────────────────────────────────
# login
# ─────────────────────────────────────────────

def _cmd_login(args, store: StorageLayout) -> int:
    platform = args.platform
    # Clean up stale QR codes on every login command
    store.cleanup_stale_qr()

    cred_path = store.credentials(platform)
    qr_path   = store.qr_image(platform)

    # XHS has a separate login flow
    if platform == "xiaohongshu":
        return _xhs_login(args, store)

    client = None
    try:
        with session_lock(cred_path):
            if args.poll:
                # Poll: need existing QR session
                if not cred_path.exists():
                    _out({"status": "error", "message": "还没有生成二维码，请先执行 login 不带 --poll"})
                    return 1
                client = _load_client(platform, store)
                result = client.poll_login()
                client.save(cred_path)
            else:
                # Start or refresh QR
                if cred_path.exists():
                    client = _load_client(platform, store)
                else:
                    client = _new_client(platform)
                out = client.start_login(qr_path, refresh=args.refresh)
                client.save(cred_path)
                result = out if isinstance(out, dict) else {
                    "status": "waiting",
                    "qr_image": str(qr_path),
                    "message": f"请用 {platform} App 扫描二维码，扫完后执行: mutipubcli login {platform} --poll",
                }
    except (FileNotFoundError, PermissionError, ValueError) as exc:
        _out({"status": "error", "message": str(exc)})
        return 1
    except HTTPFailure as exc:
        _out(exc.as_dict())
        return 1
    finally:
        _close(client)

    _out(result)
    return 0 if result.get("status") not in ("error", "expired", "failed") else 1


def _xhs_login(args, store: StorageLayout) -> int:
    platform = "xiaohongshu"
    cred_path = store.credentials(platform)
    qr_path   = store.qr_image(platform)
    method    = getattr(args, "method", "qr")

    from .platforms.xiaohongshu.login import XHSPCLogin as XHSLogin

    client = None
    try:
        ref = store.root / ".storage" / "references" / "xhs-api"
        with session_lock(cred_path):
            if method == "sms":
                if args.confirm:
                    code = input("请输入短信验证码: ").strip()
                    client = XHSLogin.load(cred_path, source=ref) if cred_path.exists() else XHSLogin(cred_path, source=ref)
                    result = client.confirm_sms(code)
                else:
                    phone = input("请输入手机号（含国家码，如 +86 138...）: ").strip()
                    client = XHSLogin.load(cred_path, source=ref) if cred_path.exists() else XHSLogin(cred_path, source=ref)
                    result = client.send_sms(phone)
            else:
                if args.poll:
                    client = XHSLogin.load(cred_path, source=ref) if cred_path.exists() else XHSLogin(cred_path, source=ref)
                    result = client.poll_login()
                else:
                    client = XHSLogin.load(cred_path, source=ref) if cred_path.exists() else XHSLogin(cred_path, source=ref)
                    out = client.start_login(qr_path)
                    result = out if isinstance(out, dict) else {
                        "status": "waiting",
                        "qr_image": str(qr_path),
                        "message": "请用小红书 App 扫描二维码，扫完后执行: mutipubcli login xiaohongshu --poll",
                    }
                client.save()
    except (FileNotFoundError, PermissionError, ValueError) as exc:
        _out({"status": "error", "message": str(exc)})
        return 1
    except HTTPFailure as exc:
        _out(exc.as_dict())
        return 1
    finally:
        _close(client)

    _out(result)
    return 0 if result.get("status") not in ("error", "expired", "failed") else 1


# ─────────────────────────────────────────────
# check
# ─────────────────────────────────────────────

def _cmd_check(args, store: StorageLayout) -> int:
    platform = args.platform
    cred_path = store.credentials(platform)

    if not cred_path.exists():
        _out({
            "status": "unauthenticated",
            "platform": platform,
            "message": f"未找到 {platform} 凭证，请先执行: mutipubcli login {platform}",
            "credentials_path": str(cred_path),
        })
        return 1

    client = None
    try:
        client = _load_client(platform, store)
        info = client.account()

        # Enrich with local credential metadata
        try:
            meta = json.loads(cred_path.read_text())
            if "expires_at" in meta:
                info["credentials_expire_at"] = meta["expires_at"]
            if "updated_at" in meta:
                info["credentials_updated_at"] = meta["updated_at"]
        except Exception:
            pass

        _out({"status": "authenticated", "platform": platform, **info})
        return 0
    except HTTPFailure as exc:
        _out({**exc.as_dict(), "platform": platform})
        return 1
    except (FileNotFoundError, PermissionError, ValueError) as exc:
        _out({"status": "error", "platform": platform, "message": str(exc)})
        return 1
    finally:
        _close(client)


# ─────────────────────────────────────────────
# publish
# ─────────────────────────────────────────────

def _do_publish(platform: str, article: Article, store: StorageLayout, draft: bool = False) -> PublishResult:
    client = _load_client(platform, store)
    try:
        if draft:
            if not hasattr(client, "draft"):
                return PublishResult("failed", f"{platform} 暂不支持草稿模式", platform=platform)
            return client.draft(article)
        return client.publish(article)
    finally:
        _close(client)


def _cmd_publish(args, store: StorageLayout, draft: bool = False) -> int:
    platform = args.platform
    verb = "存为草稿" if draft else "发布"

    try:
        article = _load_article(args.article, args.cover)
    except ValueError as exc:
        _out({"status": "failed", "platform": platform, "message": str(exc)})
        return 2

    ledger = ResultLedger(store.results_dir)
    if not ledger.reserve(platform, article):
        _out({
            "status": "skipped",
            "platform": platform,
            "message": f"已有 {platform} 的发布记录或 24 小时额度已满，不重复提交",
        })
        return 1

    try:
        result = _do_publish(platform, article, store, draft=draft)
    except HTTPFailure as exc:
        result = PublishResult("pending", str(exc), platform=platform)
    except (FileNotFoundError, PermissionError) as exc:
        result = PublishResult("failed", str(exc), platform=platform)
    except Exception as exc:
        result = PublishResult("pending", f"{verb}过程异常，请核验：{type(exc).__name__}", platform=platform)

    ledger.save(platform, article, result)
    _out_result(result)
    return 0 if result.status in ("published", "draft") else 1


# ─────────────────────────────────────────────
# verify
# ─────────────────────────────────────────────

def _cmd_verify(args, store: StorageLayout) -> int:
    platform = args.platform
    article_id = getattr(args, "id", None)

    if not article_id:
        _out({"status": "error", "platform": platform, "message": "请通过 --id 提供文章 ID"})
        return 2

    client = None
    try:
        client = _load_client(platform, store)

        # Optionally pass original article for content fingerprint verification
        expected = None
        if getattr(args, "article", None) and getattr(args, "cover", None):
            try:
                expected = _load_article(args.article, args.cover)
            except ValueError:
                pass

        if hasattr(client, "verify"):
            result = client.verify(article_id, expected=expected)
        else:
            result = PublishResult("pending", f"{platform} 暂不支持 verify", platform=platform)

        _out_result(result)
        return 0 if result.status == "published" else 1
    except HTTPFailure as exc:
        _out({**exc.as_dict(), "platform": platform})
        return 1
    except (FileNotFoundError, PermissionError, ValueError) as exc:
        _out({"status": "error", "platform": platform, "message": str(exc)})
        return 1
    finally:
        _close(client)


# ─────────────────────────────────────────────
# status
# ─────────────────────────────────────────────

def _cmd_status(args, store: StorageLayout) -> int:
    results_dir = store.results_dir
    platform_filter = getattr(args, "platform", None)

    records = []
    for f in sorted(results_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            if platform_filter and data.get("platform") != platform_filter:
                continue
            records.append(data)
        except Exception:
            pass

    if not records:
        _out({"status": "empty", "message": "暂无发布记录", "results_dir": str(results_dir)})
    else:
        # Group by platform for readability
        by_platform: dict[str, list] = {}
        for r in records:
            p = r.get("platform", "unknown")
            by_platform.setdefault(p, []).append(r)
        print(json.dumps(by_platform, ensure_ascii=False, indent=2))
    return 0


# ─────────────────────────────────────────────
# storage (diagnostic)
# ─────────────────────────────────────────────

def _cmd_storage(args, store: StorageLayout) -> int:
    desc = store.describe()

    # Enrich with credential details per platform
    creds = {}
    for p in PLATFORMS:
        path = store.credentials(p)
        if path.exists():
            try:
                meta = json.loads(path.read_text())
                creds[p] = {
                    "exists": True,
                    "expires_at": meta.get("expires_at", "未知"),
                    "updated_at": meta.get("updated_at", "未知"),
                    "path": str(path),
                }
            except Exception:
                creds[p] = {"exists": True, "path": str(path), "note": "无法解析"}
        else:
            creds[p] = {"exists": False}

    # List pending QR codes
    qr_files = []
    if store.qr_dir.exists():
        import time
        now = time.time()
        for f in store.qr_dir.glob("*.png"):
            age = int(now - f.stat().st_mtime)
            qr_files.append({"file": f.name, "age_seconds": age, "path": str(f)})

    _out({
        "storage_root": desc["root"],
        "directories": {k: v for k, v in desc.items() if k != "root"},
        "credentials": creds,
        "qr_codes": qr_files,
    })
    return 0


# ─────────────────────────────────────────────
# Argument parser
# ─────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mutipubcli",
        description="小红书 / 知乎 / 今日头条  HTTP 原生自动化发布 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  mutipubcli login toutiao              # 生成二维码
  mutipubcli login toutiao --poll       # 轮询扫码结果
  mutipubcli login toutiao --refresh    # 刷新二维码
  mutipubcli check zhihu                # 检查登录态
  mutipubcli publish zhihu --article article.md --cover cover.jpg
  mutipubcli verify toutiao --id 7385929102934
  mutipubcli status
  mutipubcli storage                    # 查看存储状态
""",
    )
    parser.add_argument("--root", metavar="DIR", help="覆盖项目根目录（用于测试）")
    sub = parser.add_subparsers(dest="command", required=True)

    # login
    p_login = sub.add_parser("login", help="扫码或短信登录")
    p_login.add_argument("platform", choices=PLATFORMS)
    p_login.add_argument("--method", choices=("qr", "sms"), default="qr", help="登录方式（默认 qr）")
    grp = p_login.add_mutually_exclusive_group()
    grp.add_argument("--poll",    action="store_true", help="轮询一次扫码结果")
    grp.add_argument("--confirm", action="store_true", help="输入短信验证码确认（仅 sms）")
    grp.add_argument("--refresh", action="store_true", help="强制刷新二维码")

    # check
    p_check = sub.add_parser("check", help="只读检查登录态与账号信息")
    p_check.add_argument("platform", choices=PLATFORMS)

    # publish
    p_pub = sub.add_parser("publish", help="发布文章（正式公开）")
    p_pub.add_argument("platform", choices=PLATFORMS)
    p_pub.add_argument("--article", required=True, help="Markdown 稿件路径（第一行为 # 标题）")
    p_pub.add_argument("--cover",   required=True, help="封面图片路径（JPG/PNG）")

    # draft
    p_draft = sub.add_parser("draft", help="保存草稿（不公开发表）")
    p_draft.add_argument("platform", choices=("zhihu", "toutiao"))
    p_draft.add_argument("--article", required=True)
    p_draft.add_argument("--cover",   required=True)

    # verify
    p_ver = sub.add_parser("verify", help="查询已提交文章的线上状态")
    p_ver.add_argument("platform", choices=PLATFORMS)
    p_ver.add_argument("--id",      required=True, help="平台文章 ID")
    p_ver.add_argument("--article", help="原稿路径（用于内容指纹核验，可选）")
    p_ver.add_argument("--cover",   help="封面路径（与 --article 配合使用）")

    # status
    p_status = sub.add_parser("status", help="查看本地发布结果记录")
    p_status.add_argument("--platform", choices=PLATFORMS, help="只显示某平台的记录")

    # storage
    sub.add_parser("storage", help="查看内部存储目录状态与凭证有效期")

    return parser


# ─────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    store = StorageLayout(Path(args.root)) if getattr(args, "root", None) else default_storage

    dispatch = {
        "login":   lambda a: _cmd_login(a, store),
        "check":   lambda a: _cmd_check(a, store),
        "publish": lambda a: _cmd_publish(a, store, draft=False),
        "draft":   lambda a: _cmd_publish(a, store, draft=True),
        "verify":  lambda a: _cmd_verify(a, store),
        "status":  lambda a: _cmd_status(a, store),
        "storage": lambda a: _cmd_storage(a, store),
    }

    handler = dispatch.get(args.command)
    if not handler:
        parser.print_help()
        return 2
    return handler(args)


def main_cli() -> None:
    sys.exit(main())


if __name__ == "__main__":
    main_cli()
