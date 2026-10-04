"""知乎浏览器登录：从页面提取二维码，等待用户扫码后导出已核验凭证。"""
from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import qrcode

from . import cookies as zhihu_cookies
from .client import ZhihuWeb
from mulpubcli.http import HTTPFailure

LOGIN_URL = 'https://www.zhihu.com/signin'
PROFILE_NAME = 'zhihu-profile'
QR_LOGIN_MAX_WAIT_MS = 120_000


def _default_on_human_needed() -> None:
    """无调用方通知回调时提示用户扫描已生成的二维码。"""
    print('知乎登录二维码已生成，请用知乎 App 扫描。', file=sys.stderr, flush=True)


class ZhihuBrowserLogin:
    """驱动一次浏览器扫码登录，导出 Cookie 为凭证。"""

    def __init__(self, destination, *, on_human_needed=None, headless=True,
                 max_wait_ms=QR_LOGIN_MAX_WAIT_MS, qr_path=None, proxy=None):
        if not isinstance(destination, Path):
            destination = Path(destination)
        self.destination = destination
        # 扫码就绪通知；缺省提示用户扫描二维码。
        self.on_human_needed = on_human_needed or _default_on_human_needed
        self.headless = headless
        if max_wait_ms is not None and max_wait_ms <= 0:
            raise ValueError('知乎扫码等待时间必须大于 0')
        self.max_wait_ms = min(max_wait_ms or QR_LOGIN_MAX_WAIT_MS,
                               QR_LOGIN_MAX_WAIT_MS)
        self.qr_path = Path(qr_path) if qr_path else None
        self.proxy = proxy
        self.account_id = ''
        self._qr_digest = None

    # ── 登录态判定 ──────────────────────────────────────────────
    def _has_login_cookie(self, ctx) -> bool:
        """浏览器上下文里是否已出现有效的 z_c0（验证 + 登录全部完成的标志）。"""
        try:
            cookies = ctx.cookies()
        except Exception:
            return False
        return any(c.get('name') == 'z_c0' and c.get('value') for c in cookies)

    def _qr_content(self, page) -> str:
        """Read the QR value from its React canvas component."""
        canvas = page.query_selector('canvas.Qrcode-qrcode')
        if canvas is None:
            raise HTTPFailure('知乎登录页未显示二维码画布', kind='invalid_response')
        try:
            value = canvas.evaluate('''(canvas) => {
                const key = Object.keys(canvas).find(k =>
                    k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance'));
                let fiber = key ? canvas[key] : null;
                for (let depth = 0; fiber && depth < 5; depth++, fiber = fiber.return) {
                    const value = fiber.memoizedProps?.value;
                    if (typeof value === 'string') return value;
                }
                return null;
            }''')
            if not isinstance(value, str) or len(value) > 2048:
                raise ValueError('二维码原始内容不存在')
            parsed = urlsplit(value)
            if (parsed.scheme != 'https' or parsed.hostname != 'www.zhihu.com'
                    or parsed.username or parsed.password or parsed.port not in (None, 443)
                    or not parsed.path.startswith('/account/scan/login/')
                    or parsed.path == '/account/scan/login/'):
                raise ValueError('二维码原始内容不是知乎扫码登录地址')
            return value
        except (OSError, ValueError, TypeError) as exc:
            raise HTTPFailure(f'知乎二维码原始内容不可用：{exc}', kind='invalid_response') from None

    @staticmethod
    def _render_qr(value: str) -> bytes:
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H,
                           box_size=36, border=4)
        qr.add_data(value)
        qr.make(fit=True)
        output = io.BytesIO()
        qr.make_image(fill_color='#0F88EB', back_color='white').save(output, format='PNG')
        return output.getvalue()

    def _snapshot(self, page, *, required=False) -> bool:
        """Write a clean, high-resolution QR only when its payload changes."""
        if self.qr_path is None:
            if required:
                raise HTTPFailure('未指定知乎二维码输出路径', kind='local_environment')
            return False
        try:
            value = self._qr_content(page)
        except HTTPFailure:
            if required:
                raise
            return False
        digest = hashlib.sha256(value.encode('utf-8')).digest()
        if digest == self._qr_digest and self.qr_path.is_file():
            return True
        try:
            png = self._render_qr(value)
        except Exception:
            raise HTTPFailure('知乎二维码编码失败', kind='invalid_response') from None
        self.qr_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix='.zhihu-qr-', dir=self.qr_path.parent)
        try:
            with os.fdopen(fd, 'wb') as target:
                os.fchmod(target.fileno(), 0o600)
                target.write(png)
            os.replace(name, self.qr_path)
        finally:
            Path(name).unlink(missing_ok=True)
        self._qr_digest = digest
        return True

    # ── Cookie 导出 ─────────────────────────────────────────────
    def _export(self, ctx) -> dict:
        """把浏览器登录后拿到的知乎 Cookie 导出为正式凭证（与 Cookie 导入同链路核验落盘）。

        复用 zhihu_cookies 的域名过滤 / 过期剔除 / z_c0 校验 / 会话构造，
        并用 account() 实时核验登录态，核验通过才原子写入正式凭证。
        """
        records = []
        for c in ctx.cookies():
            domain = c.get('domain')
            records.append({
                'name': c.get('name'),
                'value': c.get('value'),
                'domain': domain,
                'path': c.get('path') or '/',
                'secure': bool(c.get('secure', False)),
                'expires': zhihu_cookies._expires(c.get('expires')),
                'domain_specified': True,
                'domain_initial_dot': bool(domain) and domain.startswith('.'),
                'path_specified': True,
                'http_only': bool(c.get('httpOnly', False)),
                'same_site': c.get('sameSite') if isinstance(c.get('sameSite'), str) else None,
                'host_only': bool(c.get('hostOnly', False)),
            })
        records = zhihu_cookies.zhihu_records(records)
        zhihu_cookies.require_login_cookie(records)
        session = zhihu_cookies.build_session(records)
        client = ZhihuWeb(session=session, network='direct')
        try:
            info = client.account()
            # 只写 Cookie 与账号元数据，不残留任何旧二维码登录字段。
            client.qr_token, client.qr_link = '', ''
            client.qr_expires_at, client.login_blocked = 0, False
            client.save(self.destination)
            self.account_id = info.get('id', '')
            return info
        finally:
            client.close()

    # ── perform 回调（PlaywrightLoginer 复用） ──────────────────
    def _perform(self, page, browser, ctx):
        """登录页已加载：已登录则直接导出；否则等待二维码画布就绪。"""
        if self._has_login_cookie(ctx):
            self._export(ctx)
            return False              # 已完成，无需真人
        if self.qr_path:
            self.qr_path.unlink(missing_ok=True)
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        try:
            canvas = page.wait_for_selector('canvas.Qrcode-qrcode', state='visible',
                                            timeout=15_000)
        except PlaywrightTimeoutError:
            if self._has_login_cookie(ctx):
                self._export(ctx)
                return False
            raise HTTPFailure('知乎登录页未在 15 秒内显示二维码',
                              kind='invalid_response') from None
        if canvas is None:
            raise HTTPFailure('知乎登录页未显示二维码画布', kind='invalid_response')
        for attempt in range(5):
            try:
                self._snapshot(page, required=True)
                break
            except HTTPFailure:
                if attempt == 4:
                    raise
                page.wait_for_timeout(150)
        return True                   # 等待扫码，并通知调用方二维码已就绪

    def _human_done(self, page) -> bool:
        """用户扫二维码完成登录后，服务器浏览器出现 z_c0。

        由页面自身的扫码状态响应触发；二维码自动换码时更新独立图片。
        """
        if self._has_login_cookie(page.context):
            self._export(page.context)   # 登录完成，导出凭证
            return True
        self._snapshot(page)
        return False

    @staticmethod
    def _is_qr_page_response(response) -> bool:
        parsed = urlsplit(response.url)
        if parsed.hostname != 'www.zhihu.com':
            return False
        request = response.request
        if request.resource_type == 'document':
            return True
        path = parsed.path
        prefix = '/api/v3/account/api/login/qrcode'
        return ((request.method == 'POST' and path == prefix)
                or (request.method == 'GET' and path.startswith(prefix + '/')
                    and path.endswith('/scan_info')))

    def _wait_for_page_update(self, page, remaining_ms: int | None) -> None:
        """Listen to Zhihu's own QR requests; this method sends no requests."""
        from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
        started = time.monotonic()
        try:
            page.wait_for_event('response', predicate=self._is_qr_page_response,
                                timeout=min(5_000, remaining_ms) if remaining_ms is not None
                                else 5_000)
            # Let the site's response handler update its cookies and QR component.
            settle_ms = 200 if remaining_ms is None else min(
                200, max(0, remaining_ms - int((time.monotonic() - started) * 1000)))
            if settle_ms:
                page.wait_for_timeout(settle_ms)
        except PlaywrightTimeoutError:
            # A local heartbeat checks for a completed navigation if the page
            # stopped making QR requests; it does not expire or refresh the QR.
            pass
        except PlaywrightError:
            raise HTTPFailure('知乎登录页已关闭或无法继续等待', kind='http_error') from None

    def run(self) -> dict:
        """执行知乎浏览器登录（自动抠码并被动等待扫码），返回结果摘要。"""
        from mulpubcli.browser import PlaywrightLoginer
        loginer = PlaywrightLoginer(user_agent=ZhihuWeb.USER_AGENT,
                                    storage_state_dir=self.destination.parent,
                                    profile_name=PROFILE_NAME,
                                    proxy=self.proxy)
        result = loginer.login(
            LOGIN_URL, self._perform, headless=self.headless,
            on_human_needed=self.on_human_needed, human_done=self._human_done,
            wait_after_auto_ms=0,
            wait_for_human_update=self._wait_for_page_update,
            max_human_wait_ms=self.max_wait_ms)
        if result.status == 'failed' and result.timed_out:
            if self.qr_path:
                self.qr_path.unlink(missing_ok=True)
            duration = ('2 分钟' if self.max_wait_ms == QR_LOGIN_MAX_WAIT_MS
                        else f'{self.max_wait_ms / 1000:g} 秒')
            return {'status': 'expired', 'platform': 'zhihu',
                    'account_id': self.account_id,
                    'message': f'知乎扫码等待已达 {duration}，请重新执行 login 获取新码'}
        return {'status': result.status, 'platform': 'zhihu',
                'account_id': self.account_id, 'message': result.message}
