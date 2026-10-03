"""mutipubcli — 统一多平台 HTTP 发布 CLI。

命令一览：
  mutipubcli login   <platform> [--refresh] [--method sms]
  mutipubcli session [<platform>]        # 查看本地登录状态（不请求网络）
  mutipubcli reset   <platform>          # 清理登录状态后重新登录
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
import time
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

def _load_client(platform: str, store: StorageLayout, *, proxy: str | None = None):
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
        client = ZhihuWeb.load(path)
    elif platform == "toutiao":
        from .platforms.toutiao.client import ToutiaoWeb
        client = ToutiaoWeb.load(path)
    elif platform == "xiaohongshu":
        from .platforms.xiaohongshu.client import XHSHTTP
        ref = store.root / ".storage" / "references" / "xhs-api"
        client = XHSHTTP(path, source=ref)
    else:
        raise ValueError(f"不支持的平台: {platform}")
    _apply_proxy(client, proxy)
    return client


def _new_client(platform: str, *, proxy: str | None = None):
    """Create a fresh unauthenticated client for login flows."""
    if platform == "zhihu":
        from .platforms.zhihu.client import ZhihuWeb
        client = ZhihuWeb()
    elif platform == "toutiao":
        from .platforms.toutiao.client import ToutiaoWeb
        client = ToutiaoWeb()
    else:
        raise ValueError(f"{platform} 暂不支持通过此方式初始化")
    _apply_proxy(client, proxy)
    return client


def _apply_proxy(client, proxy: str | None) -> None:
    """Route the client's HTTP session through an explicit proxy (e.g. a clean exit)."""
    if not proxy:
        return
    http = getattr(client, "http", None)
    session = getattr(http, "session", None)
    if session is None:
        raise ValueError("该平台客户端不支持代理出口")
    if not (proxy.startswith("http://") or proxy.startswith("https://")
            or proxy.startswith("socks5://") or proxy.startswith("socks5h://")
            or proxy.startswith("socks4://") or proxy.startswith("socks4a://")):
        raise ValueError("代理地址须以 http://、https:// 或 socks5(h):// 开头")
    session.proxies.update({"http": proxy, "https": proxy})


def _session_is_authenticated(path: Path) -> bool:
    """Whether a saved credential already holds a valid login session.

    The QR login API rejects authenticated sessions, so a logged-in credential cannot
    mint a new QR; it must fall back to a fresh anonymous device session instead.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if data.get("account_id"):
        return True
    return any(c.get("name") in ("z_c0", "sessionid") and c.get("value")
               for c in data.get("cookies", []))


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

def _probe_account(platform: str, store: StorageLayout, cred_path: Path, *, proxy: str | None = None) -> dict | None:
    """One-shot network probe of an authenticated credential.

    Returns an emit-able status dict when the session is confirmed usable, or None when
    the session is stale/expired so the caller can mint a fresh QR. Hard local errors
    propagate so the caller reports them and stops.
    """
    client = None
    try:
        client = _load_client(platform, store, proxy=proxy)
        info = client.account()
        client.save(cred_path)
    except HTTPFailure:
        return None
    finally:
        _close(client)
    return {"status": "authenticated", "platform": platform, **info,
            "message": "登录态有效可直接使用；如需重新登录请执行 login --refresh",
            "credential_path": str(cred_path)}


def _cmd_login(args, store: StorageLayout) -> int:
    platform = args.platform
    proxy = getattr(args, "proxy", None)
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
                client = _load_client(platform, store, proxy=proxy)
                result = client.poll_login()
                client.save(cred_path)
            else:
                # login reuses valid state and auto-refreshes stale state:
                #  - an authenticated credential is probed once and returned as usable;
                #  - if that probe fails (expired/stale), it is replaced by a fresh QR;
                #  - a pending anonymous session is resumed (device cookies + rotated QR);
                #  - an authenticated session can never mint a QR, so it must fall back to
                #    a fresh anonymous device session when a new QR is needed.
                authenticated_cred = cred_path.is_file() and _session_is_authenticated(cred_path)
                if not args.refresh and authenticated_cred:
                    probe = _probe_account(platform, store, cred_path, proxy=proxy)
                    if probe is not None:
                        _out(probe)
                        return 0 if probe.get("status") == "authenticated" else 1
                    # Stale/expired authenticated session: fall through and mint a fresh QR.
                if cred_path.is_file() and not authenticated_cred:
                    client = _load_client(platform, store, proxy=proxy)
                else:
                    client = _new_client(platform, proxy=proxy)
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
# login session status / recovery
# ─────────────────────────────────────────────

def _credential_health(path: Path, qr_path: Path) -> dict:
    if not path.exists():
        return {"status": "needs_login", "credential_path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"status": "failed", "message": f"凭证文件无法读取：{type(exc).__name__}"}
    cookies = data.get("cookies") if isinstance(data.get("cookies"), list) else []
    names = {item.get("name") for item in cookies if isinstance(item, dict)}
    qr_token = data.get("qr_token") or data.get("qr_id")
    qr_expires = data.get("qr_expires_at")
    qr_created = data.get("qr_created_at")
    qr_expired = (
        qr_token
        and (
            (isinstance(qr_expires, (int, float)) and qr_expires <= time.time())
            or (isinstance(qr_created, (int, float)) and time.time() - qr_created >= 120)
        )
    )
    if data.get("login_blocked") is True or data.get("blocked") is True:
        state = "blocked"
    elif qr_expired:
        state = "expired"
    elif qr_token or data.get("initializing"):
        state = "pending_scan"
    elif data.get("account_id") or {"z_c0", "sessionid", "sessionid_ss", "sid_tt"} & names:
        state = "authenticated"
    else:
        state = "needs_login"
    result = {"status": state, "credential_path": str(path)}
    for key in ("account_id", "user_id", "qr_expires_at", "qr_created_at", "updated_at", "expires_at", "last_error"):
        if key in data:
            result[key] = data[key]
    if qr_path.is_file():
        result["qr_image"] = str(qr_path)
        result["qr_age_seconds"] = max(0, int(time.time() - qr_path.stat().st_mtime))
    return result


def _cmd_session(args, store: StorageLayout) -> int:
    platforms = (args.platform,) if args.platform else PLATFORMS
    result = {platform: _credential_health(store.credentials(platform), store.qr_image(platform))
              for platform in platforms}
    _out({"storage_root": str(store.root), "sessions": result})
    return 0


def _cmd_reset(args, store: StorageLayout) -> int:
    path = store.credentials(args.platform)
    qr = store.qr_image(args.platform)
    removed = []
    for target in (path, qr):
        if target.is_file():
            target.unlink()
            removed.append(str(target))
    _out({"status": "reset", "platform": args.platform, "removed": removed,
          "message": "登录状态已清理，请重新执行 login"})
    return 0


# ─────────────────────────────────────────────
# publish
# ─────────────────────────────────────────────

def _do_publish(platform: str, article: Article, store: StorageLayout, draft: bool = False, *, proxy: str | None = None) -> PublishResult:
    client = _load_client(platform, store, proxy=proxy)
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
        result = _do_publish(platform, article, store, draft=draft, proxy=getattr(args, "proxy", None))
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
        client = _load_client(platform, store, proxy=getattr(args, "proxy", None))

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
  mutipubcli login toutiao              # 生成二维码 / 复用有效登录态
  mutipubcli login toutiao --refresh    # 强制刷新二维码重新登录
  mutipubcli session                    # 查看各平台本地登录状态
  mutipubcli session zhihu              # 只看知乎登录状态
  mutipubcli reset zhihu                # 清理知乎登录状态
  mutipubcli publish zhihu --article article.md --cover cover.jpg
  mutipubcli verify toutiao --id 7385929102934
  mutipubcli status
  mutipubcli storage                    # 查看存储状态
""",
    )
    parser.add_argument("--root", metavar="DIR", help="覆盖项目根目录（用于测试）")
    parser.add_argument("--proxy", metavar="URL",
                        help="HTTP(S)/SOCKS5 代理出口，例如 --proxy http://127.0.0.1:7890（用于干净出口登录）")
    sub = parser.add_subparsers(dest="command", required=True)

    # login
    p_login = sub.add_parser("login", help="扫码或短信登录")
    p_login.add_argument("platform", choices=PLATFORMS)
    p_login.add_argument("--method", choices=("qr", "sms"), default="qr", help="登录方式（默认 qr）")
    grp = p_login.add_mutually_exclusive_group()
    grp.add_argument("--poll",    action="store_true", help="轮询一次扫码结果")
    grp.add_argument("--confirm", action="store_true", help="输入短信验证码确认（仅 sms）")
    grp.add_argument("--refresh", action="store_true", help="强制刷新二维码")

    # session
    p_session = sub.add_parser("session", help="查看本地登录状态（不请求网络）")
    p_session.add_argument("platform", nargs="?", choices=PLATFORMS)

    # reset
    p_reset = sub.add_parser("reset", help="清理指定平台登录状态并重新登录")
    p_reset.add_argument("platform", choices=PLATFORMS)

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
        "session": lambda a: _cmd_session(a, store),
        "reset":   lambda a: _cmd_reset(a, store),
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
