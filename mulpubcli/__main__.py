"""mulpubcli — 统一多平台 HTTP 发布 CLI。

命令一览：
  mulpubcli login   <platform> [--refresh] [--method sms]
  mulpubcli session [<platform>]        # 实时探测各平台登录态（联网核验）
  mulpubcli reset   <platform>          # 清理登录状态后重新登录
  mulpubcli publish <platform> --article FILE
  mulpubcli draft   <platform> --article FILE
  mulpubcli verify  [--id ARTICLE_ID | --platform <platform>] [--json]
  mulpubcli status  [--platform <platform>]
  mulpubcli list    [<platform>] [--json]  # 本工具发布文章的跟踪列表
  mulpubcli list-delete ID [ID ...]        # 确认后取消跟踪本地文章，不删除平台文章
  mulpubcli storage            # 查看内部存储占用

支持平台: 小红书 知乎 头条 网易号 搜狐号
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .core import Article, PublishResult, remote_body_images
from .http import HTTPFailure, session_lock, private_json
from .ledger import ResultLedger
from .login_flow import wait_for_qr_login
from .storage import StorageLayout, default_storage

PLATFORMS = ("xiaohongshu", "zhihu", "toutiao", "netease", "sohu")
PLATFORM_NAMES = {"xiaohongshu": "小红书", "zhihu": "知乎", "toutiao": "今日头条",
                  "netease": "网易号", "sohu": "搜狐号"}
DISPLAY_TZ = ZoneInfo('Asia/Shanghai')
PUBLICATION_PLATFORMS = PLATFORMS
DRAFT_PLATFORMS = ("zhihu", "toutiao", "netease")
READBACK_PLATFORMS = PLATFORMS

# ─────────────────────────────────────────────
# Platform client factory
# ─────────────────────────────────────────────

def _load_client_raw(platform: str, store: StorageLayout, *, proxy: str | None = None):
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
    elif platform == "netease":
        from .platforms.netease.client import NeteaseWeb
        client = NeteaseWeb.load(path)
    elif platform == "sohu":
        from .platforms.sohu.client import SohuWeb
        client = SohuWeb.load(path)
    else:
        raise ValueError(f"不支持的平台: {platform}")
    _apply_proxy(client, proxy)
    return client


def _load_client(platform: str, store: StorageLayout, *, proxy: str | None = None,
                 auto_renew: bool = True):
    """Check Sohu/NetEase authentication before an operation, renewing when expired."""
    renewable = auto_renew and platform in ('sohu', 'netease')
    try:
        client = _load_client_raw(platform, store, proxy=proxy)
    except FileNotFoundError:
        if not renewable:
            raise
        _auto_renew_credentials(platform, store, proxy=proxy)
        client = _load_client_raw(platform, store, proxy=proxy)
    if not renewable:
        return client
    try:
        client.account()
        return client
    except HTTPFailure as exc:
        _close(client)
        if exc.kind != 'authentication_required':
            raise
    except Exception:
        _close(client)
        raise
    _auto_renew_credentials(platform, store, proxy=proxy)
    client = _load_client_raw(platform, store, proxy=proxy)
    try:
        client.account()
        return client
    except Exception:
        _close(client)
        raise


def _saved_login_pair(store: StorageLayout, platform: str) -> tuple[str, str] | None:
    path = store.login_secret(platform)
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise HTTPFailure('保存的登录信息权限无效，请将文件权限设为 600', kind='local_state_invalid')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise HTTPFailure('保存的登录信息无法读取', kind='local_state_invalid') from None
    if (not isinstance(data, dict) or any(
            not isinstance(data.get(key), str) or not data[key]
            or '\r' in data[key] or '\n' in data[key]
            for key in ('phone', 'password'))):
        raise HTTPFailure('保存的登录信息结构无效', kind='local_state_invalid')
    return data['phone'], data['password']


def _save_login_pair(store: StorageLayout, platform: str, phone: str, password: str) -> None:
    private_json(store.login_secret(platform), {'phone': phone, 'password': password})


def _auto_renew_credentials(platform: str, store: StorageLayout, *, proxy: str | None = None) -> None:
    """Reauthenticate once using a saved pair, after a confirmed expiry."""
    cred_path = store.credentials(platform)
    with session_lock(cred_path):
        # Another process may have renewed the credential before this lock was acquired.
        if cred_path.is_file():
            current = _load_client_raw(platform, store, proxy=proxy)
            try:
                current.account()
                return
            except HTTPFailure as exc:
                if exc.kind != 'authentication_required':
                    raise
            finally:
                _close(current)

        pair = _saved_login_pair(store, platform)
        if pair is None:
            phone_key, pass_key = (('NETEASE_PHONE', 'NETEASE_PASS') if platform == 'netease'
                                   else ('SOHU_PHONE', 'SOHU_PASSWORD'))
            phone, password = os.environ.get(phone_key, ''), os.environ.get(pass_key, '')
            if not phone or not password:
                raise HTTPFailure(
                    f'{PLATFORM_NAMES[platform]}登录态失效；请先执行 login {platform} --refresh '
                    '并提供手机号和密码，以便保存自动续期信息',
                    kind='authentication_required')
            pair = (phone, password)
        phone, password = pair
        expected_id = ''
        if cred_path.is_file():
            try:
                metadata = json.loads(cred_path.read_text(encoding='utf-8'))
                expected_id = str(metadata.get('wemedia_id') or metadata.get('account_id') or '')
            except (OSError, ValueError, AttributeError):
                raise HTTPFailure('旧登录凭证无法读取，停止自动续期', kind='local_state_invalid') from None

        if platform == 'netease':
            from .platforms.netease.login import NeteaseLogin
            result = NeteaseLogin(phone, password, cred_path, account_id=expected_id).run(headless=True)
        else:
            from .platforms.sohu.login import SohuLogin
            result = SohuLogin(phone, password, cred_path, account_id=expected_id).run(headless=True)
        if result.get('status') != 'ok':
            kind = 'verification_required' if result.get('status') == 'need_human' else 'authentication_required'
            raise HTTPFailure(result.get('message') or f'{PLATFORM_NAMES[platform]}自动续期未完成', kind=kind)
        _save_login_pair(store, platform, phone, password)


def _new_client(platform: str, *, proxy: str | None = None):
    """Create a fresh unauthenticated client for login flows."""
    if platform == "zhihu":
        from .platforms.zhihu.client import ZhihuWeb
        client = ZhihuWeb()
    elif platform == "toutiao":
        from .platforms.toutiao.client import ToutiaoWeb
        client = ToutiaoWeb()
    elif platform == "netease":
        from .platforms.netease.client import NeteaseWeb
        client = NeteaseWeb()
    elif platform == "sohu":
        from .platforms.sohu.client import SohuWeb
        client = SohuWeb()
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
    print(json.dumps(data, ensure_ascii=False, indent=2), flush=True)


def _out_result(result: PublishResult, *, article_id: str | None = None,
                title: str | None = None) -> None:
    _out({**asdict(result), "id": article_id, "title": title})


def _login_result(platform: str, cred_path: Path, *, message: str,
                  info: dict | None = None) -> dict:
    """Read only non-secret identity/expiry fields from the verified credential."""
    from .login_flow import cookie_expiry_metadata
    try:
        credential = json.loads(cred_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        credential = {}
    info = info or {}
    cookies = credential.get('cookies') if isinstance(credential.get('cookies'), list) else []
    expiry = cookie_expiry_metadata(cookies, auth_names={
        'netease': ('NTESwebSI',),
    }.get(platform, ()))
    result = {'status': 'authenticated', 'platform': platform,
            'account_id': credential.get('account_id') or credential.get('wemedia_id')
                          or info.get('id') or info.get('account_id'),
            'username': credential.get('username') or info.get('name') or info.get('username'),
            'expires_at': credential.get('expires_at') or expiry['expires_at'],
            'cookie_expirations': credential.get('cookie_expirations') or expiry['cookie_expirations'],
            'credential_path': str(cred_path), 'message': message}
    details = info.get('account_details')
    if isinstance(details, dict) and details:
        result['account_details'] = details
    return result


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
        client = _load_client(platform, store, proxy=proxy, auto_renew=False)
        info = client.account()
        client.save(cred_path)
    except HTTPFailure as exc:
        if exc.kind in ('authentication_required', 'account_mismatch',
                        'verification_required') or exc.code == 40352:
            return None
        raise
    finally:
        _close(client)
    return _login_result(platform, cred_path, info=info,
                         message='登录态有效可直接使用；如需重新登录请执行 login --refresh')


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

    # 知乎登录已切为真实浏览器扫码，不再走旧二维码轮询或手动导入 Cookie。
    if platform == "zhihu":
        return _zhihu_login(args, store)

    # 网易号：瞬时无头浏览器账号密码登录（默认，自刷新）；--cookie-file 为兜底。
    if platform == "netease":
        return _netease_login(args, store)

    # 搜狐号浏览器登录：自动填账号密码，必要时在终端完成短信验证。
    if platform == "sohu":
        return _sohu_login(args, store)

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
                result = wait_for_qr_login(
                    platform=platform, qr_image=str(qr_path),
                    begin=lambda refresh: client.start_login(qr_path, refresh=args.refresh or refresh),
                    poll=client.poll_login, save=lambda: client.save(cred_path), emit=_out)
    except (FileNotFoundError, PermissionError, ValueError) as exc:
        _out({"status": "error", "message": str(exc)})
        return 1
    except HTTPFailure as exc:
        _out(exc.as_dict())
        return 1
    finally:
        _close(client)

    if result.get('status') == 'authenticated':
        qr_path.unlink(missing_ok=True)
        result = _login_result(platform, cred_path, message=result.get('message', '扫码登录成功'))
    else:
        result = {'platform': platform, **result}
    _out(result)
    successful_poll = args.poll and result.get('status') in ('waiting', 'scanned', 'waiting_confirmation')
    return 0 if result.get('status') == 'authenticated' or successful_poll else 1


def _xhs_login(args, store: StorageLayout) -> int:
    platform = "xiaohongshu"
    cred_path = store.credentials(platform)
    pending_path = store.auth_dir / 'xiaohongshu-login.json'
    qr_path   = store.qr_image(platform)
    method    = getattr(args, "method", "qr")

    from .platforms.xiaohongshu.login import XHSPCLogin, XHSLogin

    client = None
    try:
        ref = store.root / ".storage" / "references" / "xhs-api"
        with session_lock(cred_path):
            if method == "sms":
                # The creator SMS flow has its own state and API.
                sms_path = store.auth_dir / 'xiaohongshu-sms-login.json'
                client = XHSLogin.load(sms_path, source=ref) if sms_path.exists() else XHSLogin(sms_path, source=ref)
                if args.confirm:
                    code = input("请输入短信验证码: ").strip()
                    result = client.confirm_sms(code)
                else:
                    phone = input("请输入手机号（含国家码，如 +86 138...）: ").strip()
                    result = client.send_sms(phone)
                client.save()
                if result.get('status') == 'authenticated':
                    client.export(cred_path)
                    sms_path.unlink(missing_ok=True)
            else:
                if cred_path.is_file():
                    try:
                        data = json.loads(cred_path.read_text(encoding='utf-8'))
                    except (OSError, ValueError):
                        data = {}
                    if data.get('login') and not pending_path.exists():
                        # Resume a login session written by the previous single-file flow.
                        private_json(pending_path, data)
                    if not args.poll and not args.refresh and (data.get('cookie') or data.get('cookies')):
                        probe = _probe_credential(platform, store, proxy=getattr(args, 'proxy', None))
                        if probe['status'] == 'authenticated':
                            _out(_login_result(platform, cred_path, message='登录态有效可直接使用'))
                            return 0
                        if probe['status'] == 'unreachable':
                            _out({'platform': platform, **probe})
                            return 1
                if args.poll and not pending_path.exists():
                    raise ValueError('还没有生成小红书二维码，请先执行 login 不带 --poll')
                client = (XHSPCLogin.load(pending_path, source=ref) if pending_path.exists()
                          else XHSPCLogin(pending_path, source=ref))
                if args.poll:
                    result = client.poll_login()
                    client.save()
                else:
                    result = wait_for_qr_login(
                        platform=platform, qr_image=str(qr_path),
                        begin=lambda refresh: client.start_login(qr_path, refresh=args.refresh or refresh),
                        poll=client.poll_login, save=client.save, emit=_out)
                if result.get('status') == 'authenticated':
                    client.export(cred_path)
                    pending_path.unlink(missing_ok=True)
                    qr_path.unlink(missing_ok=True)
    except (FileNotFoundError, PermissionError, ValueError) as exc:
        _out({"status": "error", "message": str(exc)})
        return 1
    except HTTPFailure as exc:
        _out(exc.as_dict())
        return 1
    finally:
        _close(client)

    if result.get('status') == 'authenticated':
        result = _login_result(platform, cred_path, message=result.get('message', '扫码登录成功'))
    else:
        result = {'platform': platform, **result}
    _out(result)
    successful_poll = args.poll and result.get('status') in ('waiting', 'scanned', 'waiting_confirmation')
    return 0 if result.get('status') in ('authenticated', 'waiting_sms') or successful_poll else 1


# ─────────────────────────────────────────────
# zhihu browser login
# ─────────────────────────────────────────────

def _zhihu_login(args, store: StorageLayout) -> int:
    """知乎登录：一条命令完成扫码登录（Playwright Chromium），扫码成功即导出 Cookie。

    知乎登录页二维码被 WAF 挡在纯 HTTP 之外（curl_cffi 伪造指纹也会被重定向到
    /account/unhuman），只能用真实浏览器拿到；再用真实浏览器把二维码抠出来给用户扫。
    扫码后自动导出 Cookie。
    """
    platform = "zhihu"
    cred_path = store.credentials(platform)
    if not getattr(args, 'refresh', False) and cred_path.is_file():
        try:
            probe = _probe_account(platform, store, cred_path,
                                   proxy=getattr(args, 'proxy', None))
        except HTTPFailure as exc:
            _out(exc.as_dict())
            return 1
        if probe is not None:
            _out(probe)
            return 0
    return _zhihu_browser_login(store, cred_path,
                                refresh=getattr(args, 'refresh', False),
                                proxy=getattr(args, "proxy", None))


def _clear_zhihu_browser_profile(cred_path: Path) -> Path | None:
    from .platforms.zhihu.browser_login import PROFILE_NAME
    profile = cred_path.parent / PROFILE_NAME
    if profile.is_symlink():
        raise ValueError('知乎浏览器配置目录不能是符号链接')
    if not profile.is_dir():
        return None
    shutil.rmtree(profile)
    return profile


def _zhihu_browser_login(store: StorageLayout, cred_path: Path, *, proxy=None,
                         refresh=False) -> int:
    """一条命令完成知乎扫码登录：拉起浏览器→抠出二维码→等待页面事件即导出。

    不需要第二个终端或转发器，也不起 HTTP 服务：登录页二维码由
    _perform 抠出存到 qr_image，在 _human 回调输出与 xhs/toutiao 一致的
    waiting JSON 让用户扫码；扫完检测到 z_c0 即导出正式凭证。
    """
    platform = "zhihu"
    qr_path = store.qr_image(platform)
    from .platforms.zhihu.browser_login import ZhihuBrowserLogin

    def _human() -> None:
        # 二维码此时已在 _perform 里抠好。
        _out({"status": "waiting", "platform": platform,
              "qr_image": str(qr_path),
              "message": "请在 2 分钟内用知乎 App 扫描二维码，图片位于 qr_image"})

    loginer = ZhihuBrowserLogin(cred_path, qr_path=qr_path, proxy=proxy,
                                on_human_needed=_human)
    try:
        with session_lock(cred_path):
            if refresh:
                _clear_zhihu_browser_profile(cred_path)
            res = loginer.run()
        if res.get("status") != "ok":
            _out({"status": res.get('status', 'error'), "platform": platform,
                  "message": res.get("message", "知乎浏览器登录未完成")})
            return 1
        qr_path.unlink(missing_ok=True)
        _out(_login_result(platform, cred_path, info=res,
                           message='知乎浏览器登录成功，登录态已生效'))
        return 0
    except HTTPFailure as exc:
        _out(exc.as_dict())
        return 1
    except (OSError, ValueError) as exc:
        _out({'status': 'error', 'platform': platform, 'message': str(exc)})
        return 1


def _netease_login(args, store: StorageLayout) -> int:
    """网易号登录：瞬时无头浏览器账号密码登录（默认，自刷新）。

    走 mulpubcli.browser 拉起瞬时 Chromium 自动填网易 URS 手机号+密码，成功导出
    会话 Cookie 后立即关闭浏览器，随后纯 HTTP account() 核验写回凭证；
    登录成功时将手机号与密码保存到独立的本地登录信息文件，供凭证失效后续期。
    已有有效登录态时直接复用；失效时自动用账号密码重新登录（self refresh）。
    """
    platform = "netease"
    cred_path = store.credentials(platform)

    # 复用现有有效登录态（self refresh 前先探测）。
    if not getattr(args, "refresh", False) and cred_path.is_file():
        probe = _probe_account(platform, store, cred_path,
                               proxy=getattr(args, "proxy", None))
        if probe is not None:
            _out(probe)
            return 0 if probe.get("status") == "authenticated" else 1

    # 账号密码来源：参数 → 环境变量 → 已保存的登录信息 → 交互输入。
    try:
        saved = _saved_login_pair(store, platform) or ('', '')
    except HTTPFailure as exc:
        _out(exc.as_dict())
        return 1
    phone = getattr(args, "phone", None) or os.environ.get("NETEASE_PHONE", "") or saved[0]
    password = getattr(args, "password", None) or os.environ.get("NETEASE_PASS", "") or saved[1]
    if not phone:
        phone = input("请输入网易号手机号: ").strip()
    if not password:
        import getpass
        password = getpass.getpass("请输入网易号密码（不回显）: ")

    from .platforms.netease.login import NeteaseLogin
    try:
        print('[网易] 正在打开登录页并核验账号…', file=sys.stderr, flush=True)
        result = NeteaseLogin(phone, password, cred_path).run(headless=True)
        if result.get('status') == 'ok':
            _save_login_pair(store, platform, phone, password)
            _out(_login_result(platform, cred_path, info=result,
                               message=result.get('message', '网易浏览器登录成功')))
            return 0
        _out(result)
        return 1
    except HTTPFailure as exc:
        _out(exc.as_dict())
        return 1
    except Exception as exc:
        _out({"status": "failed", "platform": platform, "message": f"网易登录异常：{exc}"})
        return 1


def _sohu_login(args, store: StorageLayout) -> int:
    """优先复用有效凭证；失效时用浏览器登录并核验账号。"""
    platform = "sohu"
    cred_path = store.credentials(platform)
    if not getattr(args, 'refresh', False) and cred_path.is_file():
        try:
            probe = _probe_account(platform, store, cred_path,
                                   proxy=getattr(args, 'proxy', None))
        except HTTPFailure as exc:
            _out(exc.as_dict())
            return 1
        if probe is not None:
            _out(probe)
            return 0

    show_browser = bool(getattr(args, 'show_browser', False))
    if show_browser and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        _out({'status': 'failed', 'platform': platform, 'kind': 'local_environment',
              'message': '当前终端没有图形显示；请在可打开浏览器窗口的桌面或图形转发终端执行'})
        return 1

    try:
        saved = _saved_login_pair(store, platform) or ('', '')
    except HTTPFailure as exc:
        _out(exc.as_dict())
        return 1
    phone = getattr(args, "phone", None) or os.environ.get("SOHU_PHONE", "") or saved[0]
    password = getattr(args, "password", None) or os.environ.get("SOHU_PASSWORD", "") or saved[1]
    if not phone:
        phone = input("请输入搜狐手机号: ").strip()
    if not password:
        import getpass
        password = getpass.getpass("请输入搜狐密码（不回显）: ")

    from .platforms.sohu.login import SohuLogin

    try:
        print('[搜狐] 正在打开登录页并核验账号…', file=sys.stderr, flush=True)
        result = SohuLogin(phone, password, cred_path).run(headless=not show_browser)
        if result.get('status') == 'ok':
            _save_login_pair(store, platform, phone, password)
            _out(_login_result(platform, cred_path, info=result,
                               message=result.get('message', '搜狐浏览器登录成功')))
            return 0
        _out(result)
        return 1
    except HTTPFailure as exc:
        _out(exc.as_dict())
        return 1
    except Exception as exc:
        _out({"status": "failed", "platform": platform, "message": f"搜狐登录异常：{exc}"})
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
    elif data.get("account_id") or {"z_c0", "sessionid", "sessionid_ss", "sid_tt", "NTESwebSI"} & names:
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
        try:
            metadata = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            metadata = {}
        return {"status": "authenticated", "credential_path": str(path),
                "message": "登录态实时核验有效", **identity,
                "username": metadata.get('username') or (info.get('name') if platform != 'xiaohongshu' else None),
                "expires_at": metadata.get('expires_at'),
                "cookie_expirations": metadata.get('cookie_expirations', {})}
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
    targets = [path, qr]
    if args.platform in ('netease', 'sohu'):
        targets.append(store.login_secret(args.platform))
    if args.platform == 'xiaohongshu':
        targets.extend((store.auth_dir / 'xiaohongshu-login.json',
                        store.auth_dir / 'xiaohongshu-sms-login.json'))
    try:
        with session_lock(path):
            if args.platform == 'zhihu':
                profile = _clear_zhihu_browser_profile(path)
                if profile is not None:
                    removed.append(str(profile))
            for target in targets:
                if target.is_file():
                    target.unlink()
                    removed.append(str(target))
    except HTTPFailure as exc:
        _out(exc.as_dict())
        return 1
    except (OSError, ValueError) as exc:
        _out({'status': 'error', 'platform': args.platform, 'message': str(exc)})
        return 1
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


def _netease_status(status) -> str:
    """网易内容状态 → 可读状态。"""
    if status == 'deleted':
        return 'deleted'
    if isinstance(status, str) and status.startswith('published'):
        return 'published'
    if status == 'draft':
        return 'draft'
    if status == 'failed':
        return 'failed'
    return 'unknown'


def _url_id(url: str) -> str | None:
    """从文章公开链接取出平台 ID（新闻/专栏 /p/ 段）供列表使用。"""
    try:
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        last = parsed.path.rstrip('/').rsplit('/', 1)[-1]
        if parsed.hostname == 'www.sohu.com' and parsed.path.startswith('/a/'):
            match = re.fullmatch(r'(\d+)_\d+', last)
            return match.group(1) if match else None
        return last.removesuffix('.html') or None
    except Exception:
        return None


def _presentable_url(platform: str, url: str | None) -> str | None:
    """Hide Xiaohongshu bare note URLs, which are not reliable share links."""
    if not isinstance(url, str) or not url:
        return None
    if platform == 'xiaohongshu':
        from urllib.parse import parse_qs, urlsplit
        try:
            if not parse_qs(urlsplit(url).query).get('xsec_token'):
                return None
        except ValueError:
            return None
    return url


def _ledger_content_key(path: Path) -> str | None:
    platform, _, remainder = path.stem.partition('-')
    digest, suffix = remainder[:20], remainder[20:]
    if (platform not in PLATFORMS or len(digest) != 20
            or any(c not in '0123456789abcdef' for c in digest)):
        return None
    if suffix and (not suffix.startswith('-attempt-') or len(suffix) != 41
                   or any(c not in '0123456789abcdef' for c in suffix[9:])):
        return None
    return digest


def _shared_ledger_titles(store: StorageLayout) -> dict[str, str]:
    """Recover a legacy title only when the same content key has one known title."""
    candidates: dict[str, set[str]] = {}
    for path in store.results_dir.glob('*.json'):
        digest = _ledger_content_key(path)
        if digest is None:
            continue
        platform = path.stem.split('-', 1)[0]
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get('platform') != platform:
            continue
        title = data.get('title')
        if isinstance(title, str) and title.strip() and title != '未知':
            candidates.setdefault(digest, set()).add(title.strip())
    return {digest: next(iter(titles)) for digest, titles in candidates.items() if len(titles) == 1}


def _zhihu_ledger_records(store: StorageLayout) -> list[tuple[dict, Path, dict]]:
    """知乎无"我的已发布文章"列表接口，用本地发布台账兜底/逐条回查。

    只纳人有远端 ID/链接的记录；无 id 的占位/failed 记录不展示。返回
    (条目, 记录文件路径, 原始记录) 三元组，便于实时回查后写回最新状态。
    标题来自台账的 title 字段，旧记录缺失时标"未知"。
    """
    records: list[tuple[dict, Path, dict]] = []
    known_titles = _shared_ledger_titles(store)
    for path in sorted(store.results_dir.glob('zhihu-*.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            continue
        if not isinstance(data, dict) or data.get('tracked') is False:
            continue
        rid = data.get('remote_id')
        aid = rid if isinstance(rid, str) and rid else None
        url = data.get('url')
        if not aid and isinstance(url, str) and url:
            aid = _url_id(url)
        if not aid or not isinstance(aid, str) or not aid:
            continue
        item = {"id": aid, "tracking_id": path.stem,
                "title": data.get('title') or known_titles.get(_ledger_content_key(path)) or '未知',
                "status": data.get('status') or 'unknown', "url": url,
                "published_at": _fmt_time(data.get('published_at') or data.get('reserved_at') or
                                          data.get('saved_at')),
                "check": '本地记录；本次未在线回查'}
        if item['status'] == 'draft':
            item['url'] = f'https://zhuanlan.zhihu.com/p/{aid}/edit'
        records.append((item, path, data))
    return records


def _zhihu_ledger_items(store: StorageLayout) -> list[dict]:
    return [r[0] for r in _zhihu_ledger_records(store)]


def _list_zhihu(store: StorageLayout, *, proxy: str | None = None) -> dict:
    """Read back each tracked Zhihu article, including saved content evidence."""
    records = _zhihu_ledger_records(store)
    if not records:
        return {"source": "ledger", "items": []}
    try:
        client = _load_client("zhihu", store, proxy=proxy)
    except (HTTPFailure, FileNotFoundError, ValueError):
        return {"source": "ledger", "items": [r[0] for r in records]}
    try:
        ledger = ResultLedger(store.results_dir)
        items: list[dict] = []
        for item, _path, _data in records:
            try:
                evidence = ledger.verification_evidence('zhihu', item['id'])
                checked = client.verify(item['id'], evidence=evidence)
                if checked.status in ('published', 'draft', 'deleted') or checked.verification == 'mismatch':
                    item.update(status=checked.status, verification=checked.verification,
                                url=(None if checked.status == 'deleted' else checked.url or item.get('url')))
                    if checked.verification == 'verified':
                        item.pop('check', None)
                    else:
                        item['check'] = checked.message
                    ledger.reconcile('zhihu', item['id'], checked, evidence=evidence)
                else:
                    item['check'] = checked.message or '单篇回查未完成；保留本地状态'
            except (HTTPFailure, ValueError, OSError) as exc:
                item['check'] = f'单篇回查暂不可用：{exc}；保留本地状态'
            items.append(item)
        return {"source": "live", "items": items}
    finally:
        _close(client)


def _missing_deleted_rows(platform: str, store: StorageLayout, client, rows: list[dict]) -> None:
    """Check missing tracked IDs individually; only explicit deletion evidence changes status."""
    listed_ids = {str(row['id']) for row in rows if row.get('id')}
    for local in _ledger_items(platform, store):
        article_id = local.get('id')
        if not article_id or article_id in listed_ids or local.get('status') == 'deleted':
            continue
        try:
            checked = client.verify(article_id)
        except (HTTPFailure, ValueError, OSError):
            continue
        if checked.status == 'deleted':
            rows.append({'id': article_id, 'title': local['title'], 'status': 'deleted',
                         'published_at': local.get('published_at'), 'url': None,
                         'check': checked.message, 'verification': checked.verification})


def _list_platform(platform: str, store: StorageLayout, *, proxy: str | None = None) -> dict:
    """Read the platform feed and resolve tracked missing IDs when possible."""
    if platform == "zhihu":
        return _list_zhihu(store, proxy=proxy)
    client = _load_client(platform, store, proxy=proxy)
    try:
        if platform == "xiaohongshu":
            from .platforms.xiaohongshu.client import note_url
            listing = client.statuses()
            notes = listing.get('notes') or []
            items = [{"id": n.get('id'), "title": n.get('display_title'),
                      "status": _xhs_status(n),
                      "published_at": _fmt_time(n.get('time')),
                      "url": (note_url(n['id'], n.get('xsec_token') or '', n.get('xsec_source') or '')
                              if n.get('xsec_token') else None),
                      "check": (None if n.get('xsec_token') else '缺少分享令牌，暂不能生成可直接打开的链接')}
                     for n in notes if isinstance(n.get('id'), str)
                     and _is_hex24(n['id'])]
            _missing_deleted_rows(platform, store, client, items)
            return {"source": "live", "complete": listing.get('complete', False), "items": items}
        if platform == "toutiao":
            rows = client.list_articles()
            items = [{"id": r['id'], "title": r['title'],
                      "status": _toutiao_status(r.get('status')),
                      "published_at": _fmt_time(r.get('published_at')),
                      "url": f"https://www.toutiao.com/article/{r['item_id']}/" if r.get('item_id') else None}
                     for r in rows]
            _missing_deleted_rows(platform, store, client, items)
            return {"source": "live", "complete": getattr(client, '_last_list_complete', False),
                    "items": items}
        if platform == "netease":
            rows = client.list_articles()
            items = [{"id": r['id'], "title": r['title'],
                      "status": _netease_status(r.get('status')),
                      "published_at": _fmt_time(r.get('published_at')),
                      "check": ('网易作品列表显示文章已下线或删除' if _netease_status(r.get('status')) == 'deleted'
                                else None),
                      "url": (f"https://mp.163.com/subscribe_v4/index.html#/article-publish/{r['id']}"
                              if _netease_status(r.get('status')) == 'draft'
                              else f"https://www.163.com/dy/article/{r['id']}.html"
                              if _netease_status(r.get('status')) == 'published' else None)}
                     for r in rows]
            _missing_deleted_rows(platform, store, client, items)
            return {"source": "live", "complete": getattr(client, '_last_list_complete', False),
                    "items": items}
        if platform == "sohu":
            rows = client.list_articles()
            listed_ids = {str(row['id']) for row in rows}
            for local in _ledger_items('sohu', store):
                article_id = local.get('id')
                if not article_id or article_id in listed_ids or not article_id.isdigit():
                    continue
                try:
                    detail = client.article_detail(article_id)
                except (HTTPFailure, ValueError, OSError) as exc:
                    detail = {**local, 'check': f'搜狐单篇详情暂不可读：{exc}；保留本地状态'}
                if detail is None:
                    detail = {**local, 'check': '搜狐作品列表和单篇详情均未找到；不能据此断定已删除'}
                rows.append(detail)
            items = [{"id": row['id'], "title": row['title'], "status": row['status'],
                      "published_at": _fmt_time(row.get('published_at')), "url": row.get('url'),
                      "check": row.get('check')}
                     for row in rows]
            return {"source": "live", "complete": getattr(client, '_last_list_complete', False),
                    "items": items}
    finally:
        _close(client)
    raise ValueError(f"未知平台：{platform}")


def _fmt_time(value) -> str:
    """把平台/台账里各种时间表示统一成 'YYYY-MM-DD HH:MM'，便于列表展示。"""
    if not value:
        return ''
    s = str(value)
    if s.isdigit() and len(s) in (10, 13, 16):
        try:
            divisor = {10: 1, 13: 1_000, 16: 1_000_000}[len(s)]
            return datetime.fromtimestamp(int(s) / divisor, DISPLAY_TZ).strftime('%Y-%m-%d %H:%M')
        except (OverflowError, OSError, ValueError):
            return ''
    if len(s) >= 16 and s[4] == '-' and s[7] == '-':
        try:
            stamp = datetime.fromisoformat(s.replace('Z', '+00:00'))
            if stamp.tzinfo is not None:
                stamp = stamp.astimezone(DISPLAY_TZ)
            return stamp.strftime('%Y-%m-%d %H:%M')
        except ValueError:
            return ''
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
    """把各平台的发布列表渲染成含可直接复制的链接的表格。"""
    rows: list[list] = []
    for platform in PLATFORMS:
        if platform not in per_platform:
            continue
        item = per_platform.get(platform) or {}
        src = item.get('source', 'error')
        if src == 'error':
            rows.append(['', PLATFORM_NAMES.get(platform, platform),
                         f"读取失败：{item.get('message', '')}", '', '', '', ''])
            continue
        for it in item.get('items', []):
            explanation = '；'.join(str(value) for value in (it.get('message'), it.get('check')) if value)
            rows.append([_fmt_time(it.get('published_at')) or '—', PLATFORM_NAMES.get(platform, platform),
                         it.get('title') or '未知', it.get('id') or it.get('tracking_id') or '—',
                         it.get('status') or 'unknown',
                         it.get('url') or '—', explanation or it.get('verification') or '—'])
    if not rows:
        return '（暂无发布内容）'
    rows.sort(key=lambda r: r[0], reverse=True)  # 按发布时间倒序
    return _render_table(rows, ['发布时间', '平台', '标题', '编号', '状态', '链接', '核验说明'], title='发布列表')


def _ledger_items(platform: str, store: StorageLayout) -> list[dict]:
    """Keep locally known articles visible when the remote feed is partial or unavailable."""
    items = []
    known_titles = _shared_ledger_titles(store)
    for path in sorted(store.results_dir.glob(f'{platform}-*.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get('platform') != platform or data.get('tracked') is False:
            continue
        raw_url = data.get('url') if isinstance(data.get('url'), str) else None
        url = None if data.get('status') == 'deleted' else _presentable_url(platform, raw_url)
        remote_id = data.get('remote_id') or (_url_id(raw_url) if raw_url else None)
        status = data.get('status') or 'unknown'
        if status == 'failed' and not remote_id and not raw_url:
            # Nothing was accepted remotely; keep the attempt in `status`, not the article list.
            continue
        if not url and remote_id and status != 'deleted':
            if platform == 'zhihu':
                suffix = '/edit' if status == 'draft' else ''
                url = f'https://zhuanlan.zhihu.com/p/{remote_id}{suffix}'
            elif platform == 'netease':
                url = (f'https://mp.163.com/subscribe_v4/index.html#/article-publish/{remote_id}'
                       if status == 'draft' else f'https://www.163.com/dy/article/{remote_id}.html')
        check = ('单篇回查确认该文章已删除或下架' if status == 'deleted'
                 else '缺少分享令牌，暂不能生成可直接打开的链接' if platform == 'xiaohongshu' and raw_url and not url
                 else '本地记录缺少文章 ID，需在平台列表核对' if not remote_id
                 else '本地记录；本次未确认在线状态')
        last = data.get('last_verification')
        verification = last.get('verification') if isinstance(last, dict) else None
        if isinstance(last, dict) and last.get('message') and (status == 'deleted' or verification != 'verified'):
            check = last['message']
        items.append({'id': str(remote_id) if remote_id else None, 'tracking_id': path.stem,
                      'title': data.get('title') or known_titles.get(_ledger_content_key(path)) or '未知',
                      'status': status,
                      'url': url, 'published_at': _fmt_time(data.get('published_at') or
                                                           data.get('reserved_at') or data.get('saved_at')),
                      'check': check, 'verification': verification})
    return items


def _merge_live_ledger(platform: str, store: StorageLayout, live: dict) -> dict:
    """Overlay remote states onto tracked local entries; never import remote-only work."""
    remote = {str(item['id']): item for item in live.get('items', []) if item.get('id')}
    items = []
    added = False
    for local in _ledger_items(platform, store):
        match = remote.get(str(local['id'])) if local.get('id') else None
        if match:
            merged = {**local, **match, 'tracking_id': local['tracking_id']}
            merged['published_at'] = _fmt_time(match.get('published_at') or local.get('published_at'))
            merged['url'] = (None if match.get('status') == 'deleted' else
                             _presentable_url(platform, match.get('url') or local.get('url')))
            if local.get('verification') == 'mismatch' and match.get('status') == 'published':
                merged['status'] = 'pending'
            if not match.get('check') and local.get('verification') not in (None, 'verified'):
                merged['check'] = local.get('check')
            elif merged.get('url') and not match.get('check'):
                merged.pop('check', None)
            items.append(merged)
        else:
            items.append(local)
            added = True
    result = {**live, 'items': items}
    if added and live.get('source') == 'live':
        result['source'] = 'mixed'
    return result


def _cmd_list(args, store: StorageLayout) -> int:
    platforms = (args.platform,) if args.platform else READBACK_PLATFORMS
    proxy = getattr(args, "proxy", None)
    result: dict = {}
    for platform in platforms:
        tracked = _ledger_items(platform, store)
        if not any(item.get('id') for item in tracked):
            result[platform] = {"source": "ledger", "items": tracked, "complete": False}
            continue
        if platform not in READBACK_PLATFORMS:
            result[platform] = {"source": "ledger", "status": "unsupported",
                                "message": f"{platform} 尚未接入文章在线回读",
                                "items": tracked, "complete": False}
            continue
        try:
            live = _list_platform(platform, store, proxy=proxy)
            result[platform] = _merge_live_ledger(platform, store, live)
            ledger = ResultLedger(store.results_dir)
            for item in result[platform]['items']:
                if item.get('status') == 'deleted' and item.get('id') and item.get('check'):
                    ledger.reconcile(platform, str(item['id']), PublishResult(
                        'deleted', item['check'], platform=platform,
                        verification=item.get('verification') or 'verified'))
        except (HTTPFailure, ValueError, OSError) as exc:
            fallback = [{**item, 'status': ('deleted' if item.get('status') == 'deleted' else 'unreachable'),
                         'check': (f'本次平台回查失败：{exc}；上次记录为 {item.get("status") or "未知"}，'
                                   '不能确认当前状态；链接为上次记录，未验证可访问')}
                        for item in tracked]
            result[platform] = ({"source": "ledger", "status": "unreachable", "message": str(exc),
                                 "items": fallback, "complete": False} if fallback else
                                {"source": "error", "status": "unreachable", "message": str(exc),
                                 "items": [], "complete": False})
    if getattr(args, "json", False):
        _out({"platforms": result})
    else:
        print(_list_human(result))
    return 0


def _cmd_list_delete(args, store: StorageLayout) -> int:
    """Stop tracking local submissions after an explicit batch confirmation."""
    ledger = ResultLedger(store.results_dir)
    try:
        records = ledger.tracked_records()
    except HTTPFailure as exc:
        print(f'读取跟踪记录失败：{exc}')
        return 1
    selected: list[tuple[Path, dict]] = []
    known_titles = _shared_ledger_titles(store)
    seen: set[str] = set()
    for requested in args.ids:
        matches = []
        for path, data in records:
            url = data.get('url')
            remote_id = data.get('remote_id') or (_url_id(url) if isinstance(url, str) else None)
            if requested in (path.stem, str(remote_id) if remote_id else None):
                matches.append((path, data))
        if not matches:
            print(f'未找到正在跟踪的编号：{requested}；没有删除任何记录。')
            return 1
        if len(matches) != 1:
            choices = '、'.join(path.stem for path, _ in matches)
            print(f'编号 {requested} 对应多条记录，请改用本地跟踪编号：{choices}；没有删除任何记录。')
            return 1
        path, data = matches[0]
        if path.stem not in seen:
            selected.append((path, data))
            seen.add(path.stem)
    rows = []
    for path, data in selected:
        remote_id = data.get('remote_id') or (_url_id(data.get('url')) if isinstance(data.get('url'), str) else None)
        rows.append([PLATFORM_NAMES.get(data.get('platform'), data.get('platform') or '未知'),
                     data.get('title') or known_titles.get(_ledger_content_key(path)) or '未知',
                     _fmt_time(data.get('published_at') or data.get('reserved_at') or data.get('saved_at')) or '—',
                     data.get('status') or 'unknown', str(remote_id or path.stem)])
    print(_render_table(rows, ['平台', '标题', '发布时间', '状态', '编号'], title='待取消跟踪的文章'))
    print('确认取消跟踪以上文章？输入 y 确认；直接回车或输入 n 取消：', end='', flush=True)
    if sys.stdin.readline().strip().lower() != 'y':
        print('没有删除任何跟踪记录。')
        return 0
    try:
        count = ledger.untrack_records([(path.stem, data.get('saved_at')) for path, data in selected])
    except HTTPFailure as exc:
        print(f'取消跟踪失败：{exc}；没有删除任何记录。')
        return 1
    print(f'成功删除 {count} 条跟踪记录；平台上的文章仍然保留。')
    return 0


# ─────────────────────────────────────────────
# publish
# ─────────────────────────────────────────────

def _netease_editor_media(article: Article):
    from .renderer import body_content_blocks

    images = [article.cover, *article.body_images]
    blocks = [('image', article.cover), *body_content_blocks(article)]
    return images, blocks


def _netease_browser_publish(article: Article, store: StorageLayout) -> PublishResult:
    """用瞬时浏览器发布一篇网易公开文章（图文/封面 + 受保护提交）。

    网易 publishV2 的公开提交需要 ursToken，只有 v4 真实编辑器能铸造；纯 HTTP 直发不带
    ursToken 会被风控打回受限状态。本函数把「填稿 + 传图 + 封面 + 点发布」交给瞬时浏览器
    （用完即关），并在发布结果回到 pending 待核验，避免把「平台已受理」夸大成「已公开」。
    """
    import json
    from .renderer import render
    from .platforms.netease.browser_publish import NeteaseBrowserPublish

    cred_path = store.credentials("netease")
    if not cred_path.is_file():
        return PublishResult("failed", f"尚无网易凭证（{cred_path}），请先执行: mulpubcli login netease",
                             platform="netease")
    try:
        cred = json.loads(cred_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return PublishResult("failed", f"网易凭证读取失败：{exc}", platform="netease")
    cookies = cred.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        return PublishResult("failed", "网易凭证缺少会话 Cookie，请重新执行: mulpubcli login netease",
                             platform="netease")
    # 正文按文档原始顺序拆成文字/图块交替插入，保证插图在文中对应位置而非沉底；
    # 这里只取可读文本（空 image_map 会把本地图静默略掉，正文文字保留）。
    body_images, blocks = _netease_editor_media(article)
    html = render(article, {}, cover_first=False, include_title=False)
    pb = NeteaseBrowserPublish(
        cookies, article.title, html,
        body_images=body_images,
        cover_path=article.cover,
        body_blocks=blocks,
        dest=cred_path,
    )
    try:
        result = pb.run(headless=True)
    except HTTPFailure as exc:
        status = "failed" if exc.kind == "validation" else "pending"
        return PublishResult(status, f"网易浏览器发布停止：{exc}", platform="netease")
    except Exception as exc:
        return PublishResult("pending", f"网易浏览器发布异常：{type(exc).__name__}；先核验发布记录",
                             platform="netease")
    if result.get("status") in ("ok", "published"):
        remote_id = result.get('remote_id')
        if isinstance(remote_id, str) and remote_id:
            ResultLedger(store.results_dir).checkpoint('netease', article, 'submitted', remote_id)
        # 平台已受理（publishV2 返回成功），但能否公开分发由网易审核决定，
        # 用 pending 待核验，避免把「已提交」当成「已公开」。
        url = f'https://www.163.com/dy/article/{remote_id}.html' if remote_id else None
        if result.get('status') == 'published':
            return PublishResult('published', result.get('message') or '网易已发布列表确认该文章',
                                 url=url, platform='netease', verification='published')
        return PublishResult("pending", result.get("message") or "网易公开投稿已提交，请等待核验",
                             url=url, platform="netease")
    if result.get('status') in ('pending', 'need_human'):
        return PublishResult('pending',
                             result.get('message') or '网易提交结果未确认，请核验已发布列表；不要重发',
                             platform='netease')
    return PublishResult("failed", result.get("message") or "网易浏览器发布未完成",
                         platform="netease")


def _do_publish(platform: str, article: Article, store: StorageLayout, draft: bool = False, *,
                proxy: str | None = None, declaration: str = 'none') -> PublishResult:
    client = _load_client(platform, store, proxy=proxy)
    ledger = ResultLedger(store.results_dir)
    checkpoint = lambda stage, remote_id: ledger.checkpoint(platform, article, stage, remote_id)
    try:
        if platform == 'sohu' and not draft:
            return client.publish(article, checkpoint=checkpoint, declaration=declaration)
        if platform == 'toutiao':
            return client.publish(article, checkpoint=checkpoint, public=not draft)
        if draft:
            if not hasattr(client, "draft"):
                return PublishResult("failed", f"{platform} 暂不支持草稿模式", platform=platform)
            return client.draft(article, checkpoint=checkpoint)
        if platform == "netease":
            # 公开发布要走真实编辑器的受保护提交（瞬时浏览器），纯 HTTP 直发会被风控受限。
            return _netease_browser_publish(article, store)
        return client.publish(article, checkpoint=checkpoint)
    finally:
        _close(client)


def _cmd_publish(args, store: StorageLayout, draft: bool = False) -> int:
    platform = args.platform
    verb = "存为草稿" if draft else "发布"

    supported = DRAFT_PLATFORMS if draft else PUBLICATION_PLATFORMS
    if platform not in supported:
        message = (f"{platform} 暂不支持草稿" if draft else
                   f"{platform} 图文公开发布接口尚未接入；可使用 draft 保存草稿")
        _out_result(PublishResult("failed", message, platform=platform,
                                  verification="unsupported"))
        return 2
    declaration = getattr(args, 'declaration', 'none')
    if platform != 'sohu' and declaration != 'none':
        _out_result(PublishResult('failed', '--declaration 仅用于搜狐图文投稿',
                                  platform=platform, verification='unsupported'))
        return 2

    try:
        article = _load_article(args.article)
    except (ValueError, OSError) as exc:
        _out_result(PublishResult('failed', str(exc), platform=platform))
        return 2

    if (platform == "xiaohongshu" or (platform == "netease" and not draft)) \
            and remote_body_images(article.body):
        _out_result(PublishResult('failed', '该发布方式不支持正文远程图片；请先改为本地图片路径',
                                  platform=platform), title=article.title)
        return 2

    ledger = ResultLedger(store.results_dir)
    try:
        ledger.begin_attempt(platform, article)
    except HTTPFailure as exc:
        _out_result(PublishResult('failed', str(exc), platform=platform), title=article.title)
        return 2

    try:
        result = _do_publish(platform, article, store, draft=draft,
                             proxy=getattr(args, "proxy", None), declaration=declaration)
    except HTTPFailure as exc:
        status = 'failed' if exc.kind in ('authentication_required', 'verification_required',
                                          'account_mismatch', 'local_state_invalid') else 'pending'
        result = PublishResult(status, str(exc), platform=platform)
    except (FileNotFoundError, PermissionError) as exc:
        result = PublishResult("failed", str(exc), platform=platform)
    except Exception as exc:
        result = PublishResult("pending", f"{verb}过程异常，请核验：{type(exc).__name__}", platform=platform)

    saved = ledger.save(platform, article, result)
    record = json.loads(saved.read_text(encoding="utf-8"))
    _out_result(result, article_id=record.get("remote_id"), title=article.title)
    return 0 if result.status in ("published", "draft") else 1


# ─────────────────────────────────────────────
# verify
# ─────────────────────────────────────────────

def _ledger_lookup(store: StorageLayout, article_id: str) -> tuple:
    """在本地台账里按远端 ID/链接定位文章所属平台。"""
    aid = str(article_id)
    known_titles = _shared_ledger_titles(store)
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
            return data.get('platform'), data.get('title') or known_titles.get(_ledger_content_key(f)) or '未知', data.get('url')
    return None, None, None


def _is_hex24(text: str) -> bool:
    return len(text) == 24 and all(c in '0123456789abcdef' for c in text)


def _is_netease_id(text: str) -> bool:
    # 网易 docId 形如 L8D3OR2N0556PYDT（大写字线与数字混排）。
    return len(text) in (12, 16) and all(c.isalnum() for c in text) and any(c.isupper() for c in text)


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
    if _is_netease_id(s):
        return ['netease']
    if s.isdigit():
        return ['zhihu', 'toutiao']   # 知乎优先，未命中再试头条
    return ['xiaohongshu', 'zhihu', 'toutiao', 'netease']


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
        if platform == 'netease':
            state = client.article_state(str(article_id))
            _, lt_title, _ = _ledger_lookup(store, article_id)
            mapped = {'published': 'published', 'draft': 'draft', 'not_found': 'not_found', 'unknown': 'pending'}[state]
            return {'platform': 'netease', 'id': str(article_id), 'title': lt_title or '未知',
                    'status': mapped, 'url': f"https://www.163.com/dy/article/{article_id}.html"}
        # zhihu
        state = client.article_state(str(article_id))
        _, lt_title, _ = _ledger_lookup(store, article_id)
        mapped = {'published': 'published', 'draft': 'draft', 'not_found': 'not_found', 'unknown': 'pending'}[state]
        return {'platform': 'zhihu', 'id': str(article_id), 'title': lt_title or '未知',
                'status': mapped, 'url': f"https://zhuanlan.zhihu.com/p/{article_id}"}
    finally:
        _close(client)


def _render_single(r: dict) -> str:
    headers = ['发布时间', '平台', '标题', '编号', '状态', '链接', '回查原因']
    explanation = '；'.join(str(value) for value in (r.get('message'), r.get('check')) if value)
    row = [_fmt_time(r.get('published_at')) or '—',
           PLATFORM_NAMES.get(r.get('platform'), r.get('platform', '')), r.get('title') or '未知',
           str(r.get('id') or ''), r.get('status', 'unknown'), r.get('url') or '—',
           explanation or r.get('verification') or '—']
    return _render_table([row], headers, title='单篇回查')


def _cmd_verify_one(article_id: str, platform: str | None, store: StorageLayout, *,
                    proxy, as_json: bool, article_path: str | None = None) -> int:
    """Use each adapter's full readback check and reconcile the matching local record."""
    try:
        article = _load_article(article_path) if article_path else None
    except ValueError as exc:
        result = {'id': str(article_id), 'status': 'failed', 'message': str(exc)}
        _out(result) if as_json else print(_render_single(result))
        return 2
    ledger = ResultLedger(store.results_dir)
    attempts: list[dict] = []
    for p in _find_platform_for_id(store, article_id, platform):
        _, title, old_url = _ledger_lookup(store, article_id)
        prior_deleted = any(str(item.get('id')) == str(article_id) and item.get('status') == 'deleted'
                            for item in _ledger_items(p, store))
        if p not in READBACK_PLATFORMS:
            result = {'platform': p, 'id': str(article_id), 'title': title or '未知',
                      'status': 'unsupported', 'verification': 'unsupported',
                      'message': f'{p} 尚未接入文章在线回读', 'url': None}
            _out(result) if as_json else print(_render_single(result))
            return 2
        client = None
        try:
            evidence = ledger.verification_evidence(p, str(article_id), article)
            client = _load_client(p, store, proxy=proxy)
            kwargs = {'expected': article, 'evidence': evidence}
            if p == 'toutiao':
                kwargs['draft'] = any(str(item.get('id')) == str(article_id)
                                      and item.get('status') == 'draft'
                                      for item in _ledger_items('toutiao', store))
            checked = client.verify(str(article_id), **kwargs)
            result = {**asdict(checked), 'id': str(article_id), 'title': title or (article.title if article else '未知')}
            result['url'] = (None if checked.status == 'deleted' else
                             _presentable_url(p, checked.url or old_url))
            if p == 'xiaohongshu' and checked.status != 'deleted' and not result['url']:
                result['check'] = '缺少分享令牌，暂不能生成可直接打开的链接'
            ledger.reconcile(p, str(article_id), checked, evidence=evidence)
            if prior_deleted and checked.status == 'pending' and checked.verification != 'mismatch':
                result.update(status='deleted', url=None, verification='unavailable',
                              message=f'上次已确认删除；本次回查未完成：{checked.message}')
        except (HTTPFailure, FileNotFoundError, PermissionError, ValueError) as exc:
            result = {'platform': p, 'id': str(article_id), 'title': title or '未知',
                      'status': 'unreachable', 'verification': 'unavailable',
                      'message': str(exc), 'url': _presentable_url(p, old_url)}
        finally:
            if client is not None:
                _close(client)
        attempts.append(result)
        if result['status'] in ('published', 'draft', 'failed', 'deleted'):
            break
    selected = next((r for r in attempts if r['status'] in ('published', 'draft', 'failed', 'deleted')),
                    attempts[0] if attempts else {'id': str(article_id), 'status': 'unreachable',
                                                 'message': '无法确定文章所属平台', 'url': None})
    if as_json:
        _out(selected)
    else:
        print(_render_single(selected))
    return 0 if selected['status'] in ('published', 'draft') else 1


def _persist_record_status(path: Path, data: dict, status: str) -> None:
    """把实时回查得到的权威状态写回台账记录文件。"""
    data.update(status=status, last_checked=datetime.now(timezone.utc).isoformat())
    private_json(path, data)


def _refresh_zhihu(store: StorageLayout, *, proxy) -> dict:
    result = {'platform': 'zhihu', 'published': [], 'draft': [], 'other': [],
              'unverified': [], 'deleted': []}
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
                result['unverified'].append({**item, 'check': '公开页及草稿接口未找到，未判定删除'})
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
    result = {'platform': platform, 'published': [], 'draft': [], 'other': [],
              'unverified': [], 'deleted': []}
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
    # 列表接口可能分页截断、延迟更新或只返回某些状态。缺席不是删除证据。
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
        result['unverified'].append({'platform': platform, 'id': str(rid),
                                     'title': data.get('title') or '未知', 'url': data.get('url'),
                                     'status': data.get('status') or 'unknown'})
    return result


def _refresh_platform(platform: str, store: StorageLayout, *, proxy) -> dict:
    if platform in READBACK_PLATFORMS:
        return _refresh_verified(platform, store, proxy=proxy)
    return _refresh_live(platform, store, proxy=proxy)


def _refresh_verified(platform: str, store: StorageLayout, *, proxy) -> dict:
    """Read back saved articles individually; never infer deletion from feed absence."""
    result = {'platform': platform, 'published': [], 'draft': [], 'other': [],
              'unverified': [], 'deleted': []}
    tracked = _ledger_items(platform, store)
    if not tracked:
        return result
    saved_ids = {str(item['id']) for item in tracked if item.get('id')}
    base = {'source': 'ledger', 'complete': False, 'items': []}
    if platform != 'zhihu' and saved_ids:
        try:
            base = _list_platform(platform, store, proxy=proxy)
        except (HTTPFailure, ValueError, OSError) as exc:
            base['message'] = str(exc)
    items = _merge_live_ledger(platform, store, base)['items']
    client = None
    ledger = ResultLedger(store.results_dir)
    try:
        if saved_ids:
            client = _load_client(platform, store, proxy=proxy)
        for item in items:
            remote_id = str(item.get('id') or '')
            if remote_id and remote_id in saved_ids:
                try:
                    evidence = ledger.verification_evidence(platform, remote_id)
                    kwargs = {'evidence': evidence}
                    if platform == 'toutiao':
                        kwargs['draft'] = item.get('status') == 'draft'
                    checked = client.verify(remote_id, **kwargs)
                    ledger.reconcile(platform, remote_id, checked, evidence=evidence)
                    prior_deleted = item.get('status') == 'deleted'
                    keep_deleted = prior_deleted and checked.status == 'pending' and checked.verification != 'mismatch'
                    item.update(status='deleted' if keep_deleted else checked.status,
                                verification='unavailable' if keep_deleted else checked.verification,
                                message=(f'上次已确认删除；本次回查未完成：{checked.message}'
                                         if keep_deleted else checked.message),
                                url=(None if checked.status == 'deleted' or keep_deleted else
                                     _presentable_url(platform, checked.url or item.get('url'))))
                    item.pop('check', None)
                except (HTTPFailure, ValueError, OSError) as exc:
                    prior = item.get('status') or '未知'
                    if prior != 'deleted':
                        item['status'] = 'unreachable'
                    item['check'] = f'单篇回查未完成：{exc}；上次状态为 {prior}，链接未重新验证'
                    result['unverified'].append(item)
                    continue
            else:
                item.setdefault('verification', 'status_only')
            if not item.get('url') and item.get('status') != 'deleted':
                if not remote_id:
                    item['check'] = '本地记录缺少文章 ID，需在平台列表核对'
                elif platform == 'xiaohongshu':
                    item['check'] = '缺少分享令牌，暂不能生成可直接打开的链接'
                elif platform == 'toutiao':
                    item['check'] = '平台未返回公开 item_id，暂不能生成直达链接'
                else:
                    item.setdefault('check', '缺少文章 ID 或可确认的直达链接')
            if not remote_id:
                item['verification'] = 'unavailable'
                result['unverified'].append(item)
            elif item.get('status') == 'published':
                result['published'].append(item)
            elif item.get('status') == 'draft':
                result['draft'].append(item)
            elif item.get('status') == 'deleted':
                result['deleted'].append(item)
            elif item.get('status') in ('pending', 'unreachable'):
                result['unverified'].append(item)
            else:
                result['other'].append(item)
    except (HTTPFailure, ValueError, OSError) as exc:
        for item in items:
            previous = item.get('check')
            if item.get('status') != 'deleted':
                item['status'] = 'unreachable'
            item['check'] = f'平台暂不可达：{exc}' + (f'；{previous}' if previous else '')
            result['unverified'].append(item)
    finally:
        if client is not None:
            _close(client)
    return result


def _cmd_verify_refresh(platforms: tuple, store: StorageLayout, *, proxy, as_json: bool) -> int:
    summary: dict = {}
    articles: dict = {}
    for p in platforms:
        if p not in READBACK_PLATFORMS:
            summary[p] = {'unsupported': f'{p} 尚未接入文章在线回读'}
            articles[p] = _ledger_items(p, store)
            continue
        try:
            r = _refresh_platform(p, store, proxy=proxy)
        except (HTTPFailure, ValueError, OSError) as exc:
            summary[p] = {'unreachable': str(exc)}
            articles[p] = _ledger_items(p, store)
            continue
        summary[p] = {'published': len(r['published']), 'draft': len(r['draft']),
                      'other': len(r['other']), 'unverified': len(r.get('unverified', [])),
                      'deleted': len(r['deleted'])}
        articles[p] = [*r['published'], *r['draft'], *r['deleted'], *r['other'], *r.get('unverified', [])]
    if as_json:
        out = {'refresh': summary, 'articles': articles}
        _out(out)
        return 0
    print('刷新结果：')
    for p in platforms:
        s = summary.get(p) or {}
        if 'unreachable' in s:
            print(f'  · {PLATFORM_NAMES.get(p, p)}: 不可达（{s["unreachable"]}）')
        else:
            print(f'  · {PLATFORM_NAMES.get(p, p)}: 已发布 {s.get("published", 0)}，草稿 {s.get("draft", 0)}，'
                  f'待核 {s.get("unverified", 0)}，其它 {s.get("other", 0)}')
    if any(articles.values()):
        print(_list_human({p: {'source': 'live', 'items': articles.get(p, [])} for p in platforms}))
    return 0


def _cmd_verify(args, store: StorageLayout) -> int:
    article_id = getattr(args, "id", None)
    platform = getattr(args, "platform", None)
    as_json = bool(getattr(args, "json", False))
    proxy = getattr(args, "proxy", None)
    if article_id:
        return _cmd_verify_one(article_id, platform, store, proxy=proxy, as_json=as_json,
                               article_path=getattr(args, 'article', None))
    platforms = (platform,) if platform else READBACK_PLATFORMS
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
        description="小红书 / 知乎 / 今日头条 / 网易号  HTTP 原生自动化发布 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  mulpubcli login toutiao              # 生成登录二维码（小红书、头条） / 浏览器扫码（知乎）
  mulpubcli login toutiao --refresh    # 弃用已有登录，并重新登录
  mulpubcli session                    # 查看各平台本地登录状态
  mulpubcli session zhihu              # 只看知乎登录状态
  mulpubcli reset zhihu                # 清理知乎登录状态
  mulpubcli publish zhihu --article article.md       # 发布稿件
  mulpubcli list                                     # 查看本工具正在跟踪的文章
  mulpubcli list xiaohongshu                         # 只看小红书跟踪列表
  mulpubcli list-delete 123 netease-59399ec9c0373dce1a6b  # 预览并确认取消跟踪多个编号
  mulpubcli verify --id 7385929102934               # 回查单篇文章（自动识别平台）
  mulpubcli verify --platform zhihu                # 回查正在跟踪的知乎文章
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
                     help="网易号凭 Cookie 登录的兜底：从浏览器导出的网易 Cookie 文件导入（其他平台忽略）")
    p_login.add_argument("--refresh", action="store_true",
                         help="强制刷新登录（忽略现有登录态）")
    p_login.add_argument("--phone", metavar="PHONE",
                         help="登录手机号（网易用 NETEASE_PHONE、搜狐用 SOHU_PHONE；成功登录后保存供续期）")
    p_login.add_argument("--password", metavar="PASS",
                         help="登录密码（网易用 NETEASE_PASS、搜狐用 SOHU_PASSWORD；成功登录后保存供续期）")
    p_login.add_argument("--show-browser", action="store_true",
                         help="搜狐页面要求人工验证码时显示浏览器窗口（须有图形显示）")

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
    p_pub.add_argument("--declaration", choices=("none", "fiction", "ai", "marketing", "reprint", "opinion"),
                       default="none", help="搜狐创作声明；默认 none（无需声明）")
    p_draft = sub.add_parser("draft", help="保存草稿（不公开发表）")
    p_draft.add_argument("platform", choices=PLATFORMS, metavar="PLATFORM",
                         help="仅支持 zhihu、toutiao、netease；其他平台返回 unsupported")
    p_draft.add_argument("--article", required=True, help="Markdown 稿件路径（封面用 <!-- cover: 路径 --> 指令）")
    # verify
    p_ver = sub.add_parser("verify", help="回查文章在线状态：--id 查单篇；--platform 回查该平台正在跟踪的文章")
    p_ver.add_argument("--id", help="文章 ID（全局唯一，无需同时传 --platform，自动识别所属平台）")
    p_ver.add_argument("--platform", choices=PLATFORMS, help="刷新指定平台整列表状态（缺省刷新全部平台）")
    p_ver.add_argument("--article", help="原稿路径（用于内容指纹核验，配合 --id 可选）")
    p_ver.add_argument("--json", action="store_true", help="输出原始 JSON（供机器用）")

    # status
    p_status = sub.add_parser("status", help="查看本地发布结果记录")
    p_status.add_argument("--platform", choices=PLATFORMS, help="只显示某平台的记录")

    # list
    p_list = sub.add_parser("list", help="本工具发布文章的跟踪列表及平台状态")
    p_list.add_argument("platform", nargs="?", choices=PLATFORMS, help="只显示某平台，缺省列出全部")
    p_list.add_argument("--json", action="store_true", help="输出原始 JSON（供机器用），否则渲染可读表格")
    p_list_delete = sub.add_parser("list-delete", help="确认后取消跟踪本地文章，不删除平台文章")
    p_list_delete.add_argument("ids", nargs="+", metavar="ID", help="一个或多个 list 显示的编号，以空格分隔")

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
        "list-delete": lambda a: _cmd_list_delete(a, store),
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
