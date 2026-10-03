"""mulpubcli — 统一多平台 HTTP 发布 CLI。

命令一览：
  mulpubcli login   <platform> [--refresh] [--method sms]
  mulpubcli session [<platform>]        # 实时探测各平台登录态（联网核验）
  mulpubcli reset   <platform>          # 清理登录状态后重新登录
  mulpubcli publish <platform> --article FILE
  mulpubcli draft   <platform> --article FILE
  mulpubcli verify  [--id ARTICLE_ID | --platform <platform>] [--json]
  mulpubcli status  [--platform <platform>]
  mulpubcli list    [<platform>] [--json]  # 可读表格发布列表（xhs/toutiao 实时，知乎走本地台账）
  mulpubcli storage            # 查看内部存储占用

支持平台: 小红书 知乎 头条
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .core import Article, PublishResult
from .http import HTTPFailure, session_lock, private_json
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
            f"尚无 {platform} 凭证（{path}），请先执行: mulpubcli login {platform}"
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

def _load_article(article_path: str) -> Article:
    """Load an article; cover comes only from the markdown's ``<!-- cover: -->`` directive."""
    return Article.load(Path(article_path))


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

    # 知乎已完全切换为导入浏览器导出的 Cookie 登录，不再走二维码登录。
    # 无参数时默认走无回显粘贴（终端不回显）；--cookie-file 从文件导入；两者均可加 --refresh。
    if platform == "zhihu":
        return _zhihu_cookie_login(args, store)

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
                    "message": f"请用 {platform} App 扫描二维码，扫完后执行: mulpubcli login {platform} --poll",
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
                        "message": "请用小红书 App 扫描二维码，扫完后执行: mulpubcli login xiaohongshu --poll",
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
# zhihu cookie-file login
# ─────────────────────────────────────────────

def _read_cookie_stdin() -> str:
    """读取粘贴的知乎 Cookie，终端场景无回显。

    终端交互用 getpass 关闭回显读取一整行（内容不显示在屏幕）；管道/重定向输入
    时按原文读取。绝不把 Cookie 明文输出到终端、日志或错误信息。
    """
    if sys.stdin.isatty():
        import getpass
        return getpass.getpass('粘贴知乎 Cookie 后回车（内容不回显）: ').strip()
    return sys.stdin.read().strip()


def _zhihu_cookie_login(args, store: StorageLayout) -> int:
    """导入浏览器导出的知乎 Cookie（文件或无回显粘贴），核验账号后写入正式凭证。

    无参数：终端无回显粘贴 Cookie；--cookie-file：从文件导入；两者均可加 --refresh。
    """
    platform = "zhihu"
    cred_path = store.credentials(platform)
    cookie_file = getattr(args, "cookie_file", None)

    from .platforms.zhihu.client import ZhihuWeb

    try:
        with session_lock(cred_path):
            if cookie_file:
                info = ZhihuWeb.import_cookies(Path(cookie_file), cred_path)
            else:
                # 无参数时默认走无回显粘贴（终端 getpass / 管道 stdin）
                info = ZhihuWeb.import_cookie_text(_read_cookie_stdin(), cred_path)
        # 旧二维码不再属于知乎登录流程，导入成功后顺手清理残留图片。
        store.qr_image(platform).unlink(missing_ok=True)
        _out({"status": "authenticated", "platform": platform, **info,
              "message": "Chrome Cookie 导入成功，知乎登录态已生效",
              "credential_path": str(cred_path)})
        return 0
    except (FileNotFoundError, PermissionError, ValueError, OSError) as exc:
        _out({"status": "error", "message": str(exc)})
        return 1
    except HTTPFailure as exc:
        _out(exc.as_dict())
        return 1


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


def _probe_credential(platform: str, store: StorageLayout, *, proxy: str | None = None) -> dict:
    """Live-probe a saved credential against its platform, not a static file guess.

    静态猜测会因平台凭证格式不同而误判（如小红书凭证存 cookie 字符串而非 cookies 列表，
    永远被旧逻辑当成"需要登录"）。这里实例化 client 打一次轻量已认证接口，得到实时结论；
    网络/服务端暂态标 unreachable，不误报"未登录"。
    """
    path = store.credentials(platform)
    if not path.is_file():
        return {"status": "needs_login", "credential_path": str(path)}
    client = None
    try:
        client = _load_client(platform, store, proxy=proxy)
        if platform == "xiaohongshu":
            info = client.statuses()          # 已认证接口：创作者笔记列表
            identity = {}
        else:
            info = client.account()           # 返回 {id, user_id|name,...}
            identity = {k: info[k] for k in ("id", "user_id") if info.get(k)}
        return {"status": "authenticated", "credential_path": str(path),
                "message": "登录态实时核验有效", **identity}
    except HTTPFailure as exc:
        if exc.kind in ("authentication_required", "platform_rejected", "verification_required",
                        "account_setup_required", "account_mismatch"):
            return {"status": "needs_login", "credential_path": str(path),
                    "message": f"登录态失效：{exc}"}
        return {"status": "unreachable", "credential_path": str(path),
                "message": f"登录态未知（网络或服务端暂态）：{exc}"}
    except (FileNotFoundError, PermissionError) as exc:
        return {"status": "needs_login", "credential_path": str(path), "message": str(exc)}
    finally:
        _close(client)


def _cmd_session(args, store: StorageLayout) -> int:
    platforms = (args.platform,) if args.platform else PLATFORMS
    proxy = getattr(args, "proxy", None)
    result = {platform: _probe_credential(platform, store, proxy=proxy) for platform in platforms}
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
# list（实时发布列表）
# ─────────────────────────────────────────────

def _xhs_status(note: dict) -> str:
    """小红书笔记列表状态对齐 verify() 的判定，供列表统一展示。"""
    tab = note.get('tab_status')
    if tab == 3:
        return 'failed'
    if tab == 2:
        return 'auditing'
    if tab == 1 and note.get('permission_code') == 0:
        return 'published'
    return 'unknown'


def _toutiao_status(status) -> str:
    """头条内容 status 码 → 可读状态。"""
    if status == 2:
        return 'published'
    if status == 9:
        return 'draft'
    if status == 3:
        return 'failed'
    return 'unknown'


def _url_id(url: str) -> str | None:
    """从文章公开链接取出平台 ID（新闻/专栏 /p/ 段）供列表使用。"""
    cleaned = url.rstrip('/')
    if '/p/' in cleaned:
        return cleaned.rsplit('/', 1)[-1] or None
    try:
        from urllib.parse import urlparse
        last = Path(urlparse(cleaned).path).name
        return last or None
    except Exception:
        return None


def _zhihu_ledger_records(store: StorageLayout) -> list[tuple[dict, Path, dict]]:
    """知乎无"我的已发布文章"列表接口，用本地发布台账兜底/逐条回查。

    只纳人有远端 ID/链接的记录；无 id 的占位/failed 记录不展示。返回
    (条目, 记录文件路径, 原始记录) 三元组，便于实时回查后写回最新状态。
    标题来自台账的 title 字段，旧记录缺失时标"未知"。
    """
    records: list[tuple[dict, Path, dict]] = []
    for path in sorted(store.results_dir.glob('zhihu-*.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        rid = data.get('remote_id')
        aid = rid if isinstance(rid, str) and rid else None
        url = data.get('url')
        if not aid and isinstance(url, str) and url:
            aid = _url_id(url)
        if not aid or not isinstance(aid, str) or not aid:
            continue
        item = {"id": aid, "title": data.get('title') or '未知',
                "status": data.get('status') or 'unknown', "url": url,
                "published_at": _fmt_time(data.get('last_checked') or data.get('saved_at'))}
        records.append((item, path, data))
    return records


def _zhihu_ledger_items(store: StorageLayout) -> list[dict]:
    return [r[0] for r in _zhihu_ledger_records(store)]


def _list_zhihu(store: StorageLayout, *, proxy: str | None = None) -> dict:
    """知乎发布列表：按台账 ID 逐条实时回查单篇状态。

    知乎没有列表接口，只有按 ID 查单篇。有凭证且可达时逐条探测：
      - 404（已删除）→ 项从列表消失，并把台账状态记为 deleted；
      - published / draft → 反映在线状态并回写台账；
      - 网络/服务端暂态 → 保留台账原本状态，不误标也不更新。
    凭证缺失或不可达时退化到纯台账兜底（source=ledger），不联网猜。
    """
    records = _zhihu_ledger_records(store)
    if not records:
        return {"source": "ledger", "items": []}
    try:
        client = _load_client("zhihu", store, proxy=proxy)
    except (HTTPFailure, FileNotFoundError, ValueError):
        return {"source": "ledger", "items": [r[0] for r in records]}
    try:
        items: list[dict] = []
        for item, path, data in records:
            try:
                state = client.article_state(item["id"])
            except HTTPFailure:
                state = None  # 网络/服务端暂态，保留台账原状态
            if state == "not_found":
                if data.get("status") != "deleted":
                    _persist_record_status(path, data, "deleted")
                continue  # 已删除，从发布列表消失
            if state in ("published", "draft"):
                item["status"] = state
                if data.get("status") != state:
                    _persist_record_status(path, data, state)
            items.append(item)
        return {"source": "live", "items": items}
    finally:
        _close(client)


def _list_platform(platform: str, store: StorageLayout, *, proxy: str | None = None) -> dict:
    """一个平台的动态发布列表；实时读平台时删除项自然消失。"""
    if platform == "zhihu":
        return _list_zhihu(store, proxy=proxy)
    client = _load_client(platform, store, proxy=proxy)
    try:
        if platform == "xiaohongshu":
            notes = client.statuses().get('notes') or []
            items = [{"id": n.get('id'), "title": n.get('display_title'),
                      "status": _xhs_status(n),
                      "published_at": _fmt_time(n.get('time')),
                      "url": f"https://www.xiaohongshu.com/explore/{n.get('id')}"}
                     for n in notes if n.get('id')]
            return {"source": "live", "items": items}
        if platform == "toutiao":
            rows = client.list_articles()
            items = [{"id": r['id'], "title": r['title'],
                      "status": _toutiao_status(r.get('status')),
                      "published_at": r.get('published_at'),
                      "url": f"https://www.toutiao.com/article/{r.get('item_id') or r['id']}/"}
                     for r in rows]
            return {"source": "live", "items": items}
    finally:
        _close(client)
    raise ValueError(f"未知平台：{platform}")


def _fmt_time(value) -> str:
    """把平台/台账里各种时间表示统一成 'YYYY-MM-DD HH:MM'，便于列表展示。"""
    if not value:
        return ''
    s = str(value)
    if len(s) >= 16 and s[4] == '-' and s[7] == '-':
        return s[:16].replace('T', ' ')
    if s.isdigit() and len(s) >= 10:  # 秒级 epoch → 本地时间
        try:
            return datetime.fromtimestamp(int(s)).strftime('%Y-%m-%d %H:%M')
        except (OverflowError, OSError, ValueError):
            return s
    return s


def _disp_width(text: str) -> int:
    """终端显示宽度：CJK 等宽字符按 2 格计，保证中文对齐。"""
    width = 0
    for ch in str(text):
        width += 2 if ord(ch) > 0x2E80 else 1
    return width


def _render_table(rows: list[list], headers: list[str], *, title: str | None = None) -> str:
    """美观的对齐表格；传入已经是字符串的行。"""
    table = [headers] + rows
    widths = [0] * len(headers)
    for row in table:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _disp_width(cell))
    pad = 2  # 每列左右留白
    rule = '─' * (sum(widths) + pad * len(headers) + (len(headers) - 1))
    lines: list[str] = []
    if title:
        lines.append(title)
    lines.append(rule)
    for idx, row in enumerate(table):
        cells = []
        for i, cell in enumerate(row):
            text = str(cell)
            cells.append(text + ' ' * (widths[i] - _disp_width(text)))
        lines.append(' │ '.join(cells))
        if idx == 0:
            lines.append(rule)  # 表头分隔
    lines.append(rule)
    return '\n'.join(lines)


def _list_human(per_platform: dict) -> str:
    """把各平台的发布列表渲染成一张含 发布时间/平台/标题/编号 的可读表格。"""
    rows: list[list] = []
    for platform in PLATFORMS:
        item = per_platform.get(platform) or {}
        src = item.get('source', 'error')
        if src == 'error':
            rows.append(['', platform, f"读取失败：{item.get('message', '')}", '', ''])
            continue
        for it in item.get('items', []):
            rows.append([it.get('published_at') or '—', platform,
                         it.get('title') or '未知', it.get('id') or '', it.get('status') or 'unknown'])
    if not rows:
        return '（暂无发布内容）'
    rows.sort(key=lambda r: r[0], reverse=True)  # 按发布时间倒序
    return _render_table(rows, ['发布时间', '平台', '标题', '编号', '状态'], title='发布列表')


def _cmd_list(args, store: StorageLayout) -> int:
    platforms = (args.platform,) if args.platform else PLATFORMS
    proxy = getattr(args, "proxy", None)
    result: dict = {}
    for platform in platforms:
        try:
            result[platform] = _list_platform(platform, store, proxy=proxy)
        except (HTTPFailure, ValueError, OSError) as exc:
            result[platform] = {"source": "error", "status": "unreachable", "message": str(exc)}
    if getattr(args, "json", False):
        _out({"platforms": result})
    else:
        print(_list_human(result))
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
        article = _load_article(args.article)
    except ValueError as exc:
        _out({"status": "failed", "platform": platform, "message": str(exc)})
        return 2

    ledger = ResultLedger(store.results_dir)
    forced = getattr(args, "force", False)
    if not forced and not ledger.reserve(platform, article):
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

def _ledger_lookup(store: StorageLayout, article_id: str) -> tuple:
    """在本地台账里按远端 ID/链接定位文章所属平台。"""
    aid = str(article_id)
    for f in sorted(store.results_dir.glob('*.json')):
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        rid = data.get('remote_id')
        found = rid is not None and str(rid) == aid
        url = data.get('url')
        if not found and isinstance(url, str) and url and _url_id(url) == aid:
            found = True
        if found:
            return data.get('platform'), data.get('title') or '未知', data.get('url')
    return None, None, None


def _is_hex24(text: str) -> bool:
    return len(text) == 24 and all(c in '0123456789abcdef' for c in text)


def _find_platform_for_id(store: StorageLayout, article_id: str, hint: str | None) -> list[str]:
    """推断一个全局唯一 ID 属于哪个平台，返回候选顺序。"""
    if hint:
        return [hint]
    plat, _, _ = _ledger_lookup(store, article_id)
    if plat:
        return [plat]
    s = str(article_id)
    if _is_hex24(s):
        return ['xiaohongshu']
    if s.isdigit():
        return ['zhihu', 'toutiao']   # 知乎优先，未命中再试头条
    return ['xiaohongshu', 'zhihu', 'toutiao']


def _live_one(platform: str, article_id: str, store: StorageLayout, *, proxy: str | None = None) -> dict:
    """对单篇文章做一次轻量在线状态探测，返回可读结果；平台不可达时抛异常。"""
    client = _load_client(platform, store, proxy=proxy)
    try:
        if platform == 'xiaohongshu':
            notes = client.statuses().get('notes') or []
            note = next((n for n in notes if str(n.get('id')) == str(article_id)), None)
            if note:
                return {'platform': 'xiaohongshu', 'id': str(article_id), 'title': note.get('display_title'),
                        'status': _xhs_status(note), 'published_at': _fmt_time(note.get('time')),
                        'url': f"https://www.xiaohongshu.com/explore/{article_id}"}
            return {'platform': 'xiaohongshu', 'id': str(article_id), 'status': 'not_found'}
        if platform == 'toutiao':
            rows = client.list_articles()
            row = next((r for r in rows if str(r.get('id')) == str(article_id)
                        or (r.get('item_id') and str(r['item_id']) == str(article_id))), None)
            if row:
                return {'platform': 'toutiao', 'id': str(article_id), 'title': row['title'],
                        'status': _toutiao_status(row.get('status')), 'published_at': row.get('published_at'),
                        'url': f"https://www.toutiao.com/article/{row.get('item_id') or row['id']}/"}
            return {'platform': 'toutiao', 'id': str(article_id), 'status': 'not_found'}
        # zhihu
        state = client.article_state(str(article_id))
        _, lt_title, _ = _ledger_lookup(store, article_id)
        mapped = {'published': 'published', 'draft': 'draft', 'not_found': 'not_found', 'unknown': 'pending'}[state]
        return {'platform': 'zhihu', 'id': str(article_id), 'title': lt_title or '未知',
                'status': mapped, 'url': f"https://zhuanlan.zhihu.com/p/{article_id}"}
    finally:
        _close(client)


def _render_single(r: dict) -> str:
    headers = ['发布时间', '平台', '标题', '编号', '状态', '链接']
    row = [r.get('published_at') or '—', r.get('platform', ''), r.get('title') or '未知',
           str(r.get('id') or ''), r.get('status', 'unknown'), r.get('url') or '']
    return _render_table([row], headers, title='单篇回查')


def _cmd_verify_one(article_id: str, platform: str | None, store: StorageLayout, *, proxy, as_json: bool) -> int:
    """verify --id：按全局唯一 ID 自动识别平台并回查单篇。"""
    last = None
    for p in _find_platform_for_id(store, article_id, platform):
        try:
            r = _live_one(p, article_id, store, proxy=proxy)
        except (HTTPFailure, FileNotFoundError, PermissionError, ValueError) as exc:
            last = {'platform': p, 'id': str(article_id), 'status': 'unreachable', 'message': str(exc)}
            continue
        if r.get('status') != 'not_found':
            if as_json:
                _out(r)
            else:
                print(_render_single(r))
            return 0 if r.get('status') == 'published' else 1
        last = r
    if as_json:
        _out(last or {'id': str(article_id), 'status': 'not_found'})
    else:
        print(_render_single(last or {'id': str(article_id), 'status': 'not_found'}))
    return 1


def _persist_record_status(path: Path, data: dict, status: str) -> None:
    """把实时回查得到的权威状态写回台账记录文件。"""
    data.update(status=status, last_checked=datetime.now(timezone.utc).isoformat())
    private_json(path, data)


def _refresh_zhihu(store: StorageLayout, *, proxy) -> dict:
    result = {'platform': 'zhihu', 'published': [], 'draft': [], 'other': [], 'deleted': []}
    records = _zhihu_ledger_records(store)
    if not records:
        return result
    try:
        client = _load_client('zhihu', store, proxy=proxy)
    except (HTTPFailure, FileNotFoundError, ValueError):
        return result
    try:
        for item, path, data in records:
            try:
                state = client.article_state(item['id'])
            except HTTPFailure:
                state = None
            if state == 'not_found':
                result['deleted'].append({'platform': 'zhihu', 'id': item['id'],
                                          'title': item['title'], 'url': item['url'], 'path': path})
                if data.get('status') != 'deleted':
                    _persist_record_status(path, data, 'deleted')
            elif state == 'published':
                result['published'].append(item)
                if data.get('status') != 'published':
                    _persist_record_status(path, data, 'published')
            elif state == 'draft':
                result['draft'].append(item)
            else:
                result['other'].append(item)
        return result
    finally:
        _close(client)


def _refresh_live(platform: str, store: StorageLayout, *, proxy) -> dict:
    result = {'platform': platform, 'published': [], 'draft': [], 'other': [], 'deleted': []}
    per = _list_platform(platform, store, proxy=proxy)
    if per.get('source') == 'error':
        return result
    items = per.get('items', [])
    cur = {str(i['id']) for i in items if i.get('id')}
    for i in items:
        if i.get('status') == 'published':
            result['published'].append(i)
        elif i.get('status') == 'draft':
            result['draft'].append(i)
        else:
            result['other'].append(i)
    # 平台实时列表中已消失、但台账仍记着 remote_id 的记录 → 远端已删除
    for path in sorted(store.results_dir.glob(f'{platform}-*.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        if not isinstance(data, dict) or data.get('platform') != platform:
            continue
        rid = data.get('remote_id')
        if rid is None:
            continue
        if str(rid) in cur:
            continue
        result['deleted'].append({'platform': platform, 'id': str(rid),
                                  'title': data.get('title') or '未知', 'url': data.get('url'), 'path': path})
        if data.get('status') != 'deleted':
            _persist_record_status(path, data, 'deleted')
    return result


def _refresh_platform(platform: str, store: StorageLayout, *, proxy) -> dict:
    if platform == 'zhihu':
        return _refresh_zhihu(store, proxy=proxy)
    return _refresh_live(platform, store, proxy=proxy)


def _confirm_delete(candidates: list[dict], store: StorageLayout) -> None:
    """交互式移除删除项：回车跳过，ALL 全删，或用 , 分隔编号指定删除；未删的下次再提示。"""
    rows = [[c['id'], c['platform'], c['title'] or '未知'] for c in candidates]
    print(_render_table(rows, ['编号', '平台', '标题'], title='检测到已删、可从列表移除：'))
    print('回车=不移除；ALL=全部移除；输入以 , 分隔的编号=仅移除这些')
    _raw = sys.stdin.readline()
    raw = (_raw or '').strip()
    if not raw:
        print('未移除任何项，下次回查会再次提示。')
        return
    if raw.upper() == 'ALL':
        chosen = {str(c['id']) for c in candidates}
    else:
        parts = [p.strip() for p in raw.replace('，', ',').replace(' ', '').split(',') if p.strip()]
        chosen = set(parts)
    removed = 0
    for c in candidates:
        if str(c['id']) in chosen:
            try:
                path = Path(c['path'])
                if path.is_file():
                    path.unlink()
                    removed += 1
            except OSError:
                pass
    print(f'已从列表移除 {removed} 条。')
    if removed < len(candidates):
        print('其余未移除，下次回查会再次提示。')


def _cmd_verify_refresh(platforms: tuple, store: StorageLayout, *, proxy, as_json: bool) -> int:
    summary: dict = {}
    all_deleted: list[dict] = []
    for p in platforms:
        try:
            r = _refresh_platform(p, store, proxy=proxy)
        except (HTTPFailure, ValueError, OSError) as exc:
            summary[p] = {'unreachable': str(exc)}
            continue
        summary[p] = {'published': len(r['published']), 'draft': len(r['draft']),
                      'other': len(r['other']), 'deleted': len(r['deleted'])}
        all_deleted.extend(r['deleted'])
    if as_json:
        out = {'refresh': summary}
        if all_deleted:
            out['deletion_pending'] = [{k: v for k, v in d.items() if k != 'path'} for d in all_deleted]
        _out(out)
        return 0
    print('刷新结果：')
    for p in platforms:
        s = summary.get(p) or {}
        if 'unreachable' in s:
            print(f'  · {p}: 不可达（{s["unreachable"]}）')
        else:
            print(f'  · {p}: 已发布 {s.get("published", 0)}，草稿 {s.get("draft", 0)}，'
                  f'删除 {s.get("deleted", 0)}，其它 {s.get("other", 0)}')
    if all_deleted:
        _confirm_delete(all_deleted, store)
    else:
        print('  没有检测到需要从列表移除的已删除项。')
    return 0


def _cmd_verify(args, store: StorageLayout) -> int:
    article_id = getattr(args, "id", None)
    platform = getattr(args, "platform", None)
    as_json = bool(getattr(args, "json", False))
    proxy = getattr(args, "proxy", None)
    if article_id:
        return _cmd_verify_one(article_id, platform, store, proxy=proxy, as_json=as_json)
    platforms = (platform,) if platform else PLATFORMS
    return _cmd_verify_refresh(platforms, store, proxy=proxy, as_json=as_json)


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
        prog="mulpubcli",
        description="小红书 / 知乎 / 今日头条  HTTP 原生自动化发布 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  mulpubcli login toutiao              # 生成登录二维码（小红书、头条） / 要求提供 Cookie（知乎）
  mulpubcli login toutiao --refresh    # 弃用已有登录，并重新登录
  mulpubcli session                    # 查看各平台本地登录状态
  mulpubcli session zhihu              # 只看知乎登录状态
  mulpubcli reset zhihu                # 清理知乎登录状态
  mulpubcli publish zhihu --article article.md       # 发布稿件
  mulpubcli list                                     # 查看各平台发布列表
  mulpubcli list xiaohongshu                         # 只看小红书发布列表
  mulpubcli verify --id 7385929102934               # 回查单篇文章（自动识别平台）
  mulpubcli verify --platform zhihu                # 刷新知乎，汇报变化并从列表移除已删除项
  mulpubcli verify                                 # 刷新全部平台
  mulpubcli status
  mulpubcli storage                    # 查看存储状态
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
    grp.add_argument("--cookie-file", metavar="FILE",
                     help="从文件导入浏览器导出的知乎 Cookie（原始 Cookie 头 / Chrome/Playwright JSON / cookies.txt）")
    p_login.add_argument("--refresh", action="store_true",
                         help="强制刷新二维码 / 强制重新导入知乎 Cookie（忽略现有登录态）")

    # session
    p_session = sub.add_parser("session", help="实时探测各平台登录态（联网核验凭证有效性）")
    p_session.add_argument("platform", nargs="?", choices=PLATFORMS)

    # reset
    p_reset = sub.add_parser("reset", help="清理指定平台登录状态并重新登录")
    p_reset.add_argument("platform", choices=PLATFORMS)

    # publish
    p_pub = sub.add_parser("publish", help="发布文章（正式公开）")
    p_pub.add_argument("platform", choices=PLATFORMS)
    p_pub.add_argument("--article", required=True, help="Markdown 稿件路径（第一行为 # 标题，封面用 <!-- cover: 路径 --> 指令）")
    p_pub.add_argument("--force", action="store_true",
                      help="强制发送：跳过本地 24 小时去重与 pending 记录拦截，直接重新投稿并记账")

    # draft
    p_draft = sub.add_parser("draft", help="保存草稿（不公开发表）")
    p_draft.add_argument("platform", choices=("zhihu", "toutiao"))
    p_draft.add_argument("--article", required=True, help="Markdown 稿件路径（封面用 <!-- cover: 路径 --> 指令）")
    p_draft.add_argument("--force", action="store_true",
                        help="强制发送：跳过去重拦截，直接重新提交并记账")

    # verify
    p_ver = sub.add_parser("verify", help="回查文章在线状态：--id 查单篇（无需平台）；--platform 刷新整平台变化并从列表移除已删除项")
    p_ver.add_argument("--id", help="文章 ID（全局唯一，无需同时传 --platform，自动识别所属平台）")
    p_ver.add_argument("--platform", choices=PLATFORMS, help="刷新指定平台整列表状态（缺省刷新全部平台）")
    p_ver.add_argument("--article", help="原稿路径（用于内容指纹核验，配合 --id 可选）")
    p_ver.add_argument("--json", action="store_true", help="输出原始 JSON，不进入删除交互（供机器用）")

    # status
    p_status = sub.add_parser("status", help="查看本地发布结果记录")
    p_status.add_argument("--platform", choices=PLATFORMS, help="只显示某平台的记录")

    # list
    p_list = sub.add_parser("list", help="实时发布列表（xhs/toutiao 读平台，知乎走本地台账）")
    p_list.add_argument("platform", nargs="?", choices=PLATFORMS, help="只显示某平台，缺省列出全部")
    p_list.add_argument("--json", action="store_true", help="输出原始 JSON（供机器用），否则渲染可读表格")

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
        "list":    lambda a: _cmd_list(a, store),
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
