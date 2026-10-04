"""网易号（netease）瞬时浏览器登录 — 账号密码换凭证。

网易号后台发布写接口（publishV2.do）对未知第三方工具的风控较严，登录也要求真实
浏览器会话方可长期稳定使用。本模块调用 mulpubcli.browser 共享组件**按需**拉起
无头 Chromium：自动填网易 URS 手机号 + 密码并提交，成功后导出会话 Cookie 到
持久化凭证，随后立即关闭浏览器（finally 兜底）。平时后台零进程、零内存。

安全：账号密码只从环境变量（NETEASE_PHONE / NETEASE_PASS）或 --phone/--password/
交互输入读取一次，绝不写入代码、仓库、日志或输出；落盘的是登录后的会话 Cookie
与账号 id，不是明文密码。之后 account()/draft() 走 HTTP；发布走浏览器路径。
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from mulpubcli.http import HTTPFailure

_URS = 'reg.163.com'
_LOGIN_URL = 'https://mp.163.com/'


class NeteaseLogin:
    """驱动一次网易浏览器登录并导出凭证到持久化文件。"""

    def __init__(self, phone: str, password: str, destination: Path,
                 *, account_id: str = ''):
        if not isinstance(phone, str) or not isinstance(password, str):
            raise ValueError('网易手机号与密码须为字符串')
        if '\r' in phone or '\n' in phone or '\r' in password or '\n' in password:
            raise ValueError('网易手机号或密码包含非法字符')
        self.phone = phone
        self.password = password
        self.destination = destination
        self.account_id = account_id

    # ─────────────────────────────────────────────
    # perform 回调：自动填 URS 登录表单并提交
    # ─────────────────────────────────────────────

    def _export(self, context) -> None:
        """把浏览器会话 Cookie 规约为凭证文件（账号密码不落盘）。"""
        from mulpubcli.http import add_cookies, save_session
        from .client import USER_AGENT
        import requests
        records = _convert(context.cookies())
        records = [r for r in records if _allowed(r['domain'])]
        if not records:
            return
        self.destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        session = requests.Session()
        add_cookies(session, records)
        save_session(self.destination, session, user_agent=USER_AGENT,
                     network='direct', account_id=self.account_id)

    def _perform(self, page, browser, ctx) -> bool:
        """自动完成 URS 手机密码登录；返回 True=需要真人环节，False=已完成。"""
        if 'login.html' not in page.url:
            self._export(page.context)
            return False
        # URS iframe 往往晚于顶层 DOM 出现，等它实际载入。
        frame_deadline = time.monotonic() + 10
        urs = next((f for f in page.frames if _URS in f.url), None)
        while urs is None and time.monotonic() < frame_deadline:
            page.wait_for_timeout(100)
            if 'login.html' not in page.url:
                self._export(page.context)
                return False
            urs = next((f for f in page.frames if _URS in f.url), None)
        if urs is None:
            return True
        try:
            urs.click(":text('手机号登录')", timeout=4000)
            try:
                urs.click(".tab0", timeout=2500)
            except Exception:
                pass
            urs.click("#phoneipt", timeout=4000)
            urs.type("#phoneipt", self.phone, delay=30)
            urs.press("#phoneipt", "Escape")          # 关闭账号联想下拉
            urs.click("input[placeholder='请输入密码']", timeout=4000)
            urs.type("input[placeholder='请输入密码']", self.password, delay=40)
            try:
                urs.eval_on_selector(
                    ".m-ckcnt,.ckbox",
                    "e=>{const c=e.querySelector('input[type=checkbox]')||e;"
                    " if(c && !c.checked) c.click(); return c?c.checked:null}")
            except Exception:
                pass
            urs.click(".u-loginbtn", timeout=4000)
        except Exception:
            return True   # 提交未完成，交由等待/重试路径
        # 以实际跳转或验证控件为完成信号；短间隔仅用于检测跨域 iframe 状态。
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if 'login.html' not in page.url:
                self._export(page.context)
                return False
            try:
                yidun = urs.eval_on_selector_all(
                    ".yidun,.yidun_panel,iframe[src*=dun]", "e=>e.length")
            except Exception:
                yidun = 0
            if yidun:
                return True
            page.wait_for_timeout(250)
        return True

    # ─────────────────────────────────────────────
    # 入口
    # ─────────────────────────────────────────────

    def run(self, *, headless: bool = True) -> dict:
        """执行一次登录，返回凭证摘要；浏览器用完即关。"""
        from mulpubcli.browser import PlaywrightLoginer
        from .client import NeteaseWeb

        target = self.destination
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix='.netease-login-', suffix='.json', dir=target.parent)
        os.close(fd)
        staged = Path(name)
        staged.unlink()
        self.destination = staged
        try:
            loginer = PlaywrightLoginer(storage_state_dir=target.parent)
            result = loginer.login(_LOGIN_URL, self._perform, headless=headless,
                                   wait_after_auto_ms=0, wait_between_checks_ms=1000,
                                   max_human_wait_ms=300_000, viewport=(1360, 900))
            exported = {'platform': 'netease', 'message': result.message}
            if result.status != 'ok':
                exported['status'] = result.status
                return exported
            if not staged.is_file():
                return {'platform': 'netease', 'status': 'failed',
                        'message': '浏览器未导出网易登录凭证'}
            # 先用纯 HTTP 核验暂存凭证，通过后才替换现有登录态。
            try:
                client = NeteaseWeb.load(staged)
                try:
                    info = client.account()
                    client.save(staged)
                finally:
                    client.close()
            except HTTPFailure as exc:
                return {'platform': 'netease', 'status': 'failed',
                        'message': f'凭证已导出但核验失败：{exc}'}
            os.replace(staged, target)
            return {'platform': 'netease', 'status': 'ok',
                    'account_id': info.get('id', ''),
                    'username': info.get('name', ''),
                    'account_details': info.get('account_details', {}),
                    'message': '浏览器登录成功，网易号登录态已生效'}
        finally:
            self.destination = target
            staged.unlink(missing_ok=True)


def _allowed(domain: str) -> bool:
    """是否网易号允许的 Cookie 目标域名，与 cookies 模块一致。"""
    d = domain.lstrip('.')
    return d == '163.com' or d.endswith('.163.com')


def _convert(playwright_cookies) -> list[dict]:
    """把 Playwright context.cookies() 规约为项目凭证记录。"""
    out = []
    for c in playwright_cookies:
        domain = c.get('domain', '')
        if not isinstance(domain, str) or '.' not in domain:
            continue
        out.append({
            'name': c['name'], 'value': c['value'], 'domain': domain,
            'path': c.get('path', '/'), 'secure': bool(c.get('secure')),
            'expires': c.get('expires', -1),
            'domain_specified': True, 'domain_initial_dot': domain.startswith('.'),
            'path_specified': bool(c.get('path')),
        })
    return out


__all__ = ['NeteaseLogin']
