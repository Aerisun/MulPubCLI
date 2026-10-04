"""搜狐号浏览器登录：填账号密码，必要时完成短信验证，再核验并保存凭证。"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

from .client import SohuWeb, MP, USER_AGENT
from mulpubcli.http import HTTPFailure, save_session


LOGIN_URL = 'https://v4.passport.sohu.com/fe/login?appid=999801&p=password'
EDITOR_URL = MP + '/mpfe/v4/contentManagement/news/addarticle?contentStatus=2'
class SohuLogin:
    """驱动一次搜狐登录并导出凭证到持久化文件。"""

    def __init__(self, phone: str, password: str, destination: Path,
                 *, account_id: str = ''):
        if not isinstance(phone, str) or not isinstance(password, str):
            raise ValueError('搜狐手机号与密码须为字符串')
        if '\r' in phone or '\n' in phone or '\r' in password or '\n' in password:
            raise ValueError('搜狐手机号或密码包含非法字符')
        self.phone = phone
        self.password = password
        self.destination = destination
        self.account_id = account_id
        self.dv_id = ''

    def _solve_sms_code(self, page, code_timeout_s: int = 240) -> bool:
        """等用户输入短信验证码并提交。找不到输入框则跳过。

        优先从终端 getpass 读；非 tty / 无人值守（由外部网页服务或上层托管）时，
        轮询临时文件 code_file（换行分隔每位或整串）读取。——文件由调用方或用户写入。
        """
        try:
            code_input = page.locator("input[type=text]").last
            if code_input.count() == 0:
                return False
        except Exception:
            return False
        # 1) 点"获取验证码"把短信发出去（clientAuth 页按钮文本即"获取验证码"）。
        for sel in ("text=获取验证码", "button:has-text('获取验证码')",
                    "text=重新获取", "text=获取验证码?", "text=获取"):
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    loc.click(timeout=5000)
                    break
            except Exception:
                continue
        # 2) 读短信验证码（终端 getpass 优先，非 tty 直接回退到临时文件轮询）。
        code_file = Path(self.destination.parent) / '.dev' / 'sohu_sms_code.txt'
        code = ''
        if sys.stdin.isatty():
            try:
                import getpass
                code = getpass.getpass('请输入搜狐短信验证码（6 位，不回显）: ').strip()
            except (EOFError, KeyboardInterrupt, OSError):
                code = ''
        if not code:
            code_file.parent.mkdir(parents=True, exist_ok=True)
            print(f'[搜狐] 等待短信验证码… 将 6 位验证码写入 {code_file}（最多等 {code_timeout_s}s）',
                  file=sys.stderr, flush=True)
            deadline = time.time() + code_timeout_s
            last = None
            while time.time() < deadline and not code:
                try:
                    if code_file.exists():
                        cur = code_file.read_text(encoding='utf-8').strip()
                    else:
                        cur = ''
                except Exception:
                    cur = ''
                if cur and cur != last:
                    code = ''.join(ch for ch in cur if ch.isdigit())
                last = cur
                time.sleep(1)
            if code and code_file.exists():
                try:
                    code_file.unlink()
                except Exception:
                    pass
        if not code:
            print('[搜狐] 未收到短信验证码，跳过短信提交。', file=sys.stderr, flush=True)
            return False
        try:
            code_input.fill(code)
        except Exception:
            return False
        # 点"提交/确定"按钮。
        for i in range(page.locator('button').count()):
            try:
                text = page.locator('button').nth(i).inner_text() or ''
            except Exception:
                continue
            if '提交' in text or '确定' in text or '确认' in text:
                try:
                    page.locator('button').nth(i).click(timeout=5000)
                except Exception:
                    pass
                break
        try:
            page.wait_for_function(
                "() => !location.href.includes('/clientAuth') || "
                "!document.body.innerText.includes('短信验证')",
                timeout=15_000)
        except Exception:
            pass
        return True

    @staticmethod
    def _wait_for_editor_or_auth(page) -> None:
        """等后台编辑器或设备授权页实际出现，再判断下一步。"""
        page.wait_for_function(
            "() => location.href.includes('/clientAuth') || "
            "(location.hostname === 'mp.sohu.com' && "
            "location.pathname.includes('/contentManagement/') && "
            "document.readyState === 'complete')",
            timeout=15_000)

    def _perform(self, page, browser, ctx):
        """执行登录自动化。返回 True 表示仍需外部帮助，False 表示已全部完成。"""
        from playwright.sync_api import TimeoutError as PWTimeout
        # 第一步：填手机号 + 密码并提交。
        page.fill("input[type=text]:nth-of-type(1)", self.phone)
        page.fill("input[type=password]:nth-of-type(1)", self.password)
        button = page.locator(".login-button").first
        button.wait_for(state='visible', timeout=8000)
        button.click(timeout=8000)
        # 页面跳转是登录成功的信号；超时后仍进入后台，由账号接口判定结果。
        try:
            page.wait_for_function(
                "() => !location.href.includes('/fe/login') || "
                "!document.querySelector('.login-button')",
                timeout=12_000)
        except PWTimeout:
            pass
        # 进入搜狐号后台；若未授权会被重定向到 clientAuth 授权页。
        try:
            page.goto(EDITOR_URL, timeout=50000, wait_until='domcontentloaded')
            self._wait_for_editor_or_auth(page)
        except PWTimeout:
            pass

        # 判断是否需真人授权：URL 停在 clientAuth 或页面含"短信验证"字样。
        try:
            body_text = page.evaluate("document.body.innerText")
        except Exception:
            body_text = ''
        need_human = '/clientAuth' in (page.url or '') or '短信验证' in body_text
        self._need_human = need_human

        # 第二步：搜狐要求设备授权时，由终端输入短信验证码。
        if need_human:
            self._solve_sms_code(page)
            # 短信通过后，回到编辑器；已授权设备不会再被拦。
            try:
                page.goto(EDITOR_URL, timeout=50000, wait_until='domcontentloaded')
                self._wait_for_editor_or_auth(page)
            except PWTimeout:
                pass

        # 暂存浏览器凭证；run() 会再用账号接口核验，通过才替换正式凭证。
        self._export(page)
        return False

    def _export(self, page):
        """从浏览器上下文导出会话 cookie + localStorage dv-id + mp-cv → 持久化。"""
        cookies = page.context.cookies()
        jar = {}
        for c in cookies:
            dom = c['domain'].lstrip('.')
            jar.setdefault(dom, {})[c['name']] = c['value']
        dv = ''
        try:
            dv = page.evaluate("localStorage.getItem('preview-dv-id') || ''")
        except Exception:
            dv = ''
        sohu = jar.get('sohu.com', {})
        mp = jar.get('mp.sohu.com', {})
        sp_cm = mp.get('mp-cv', '')
        self.dv_id = dv
        # 保留浏览器给出的真实域名、路径、安全属性和过期时间。
        records = [
            {'name': c['name'], 'value': c['value'], 'domain': c['domain'],
             'path': c.get('path', '/'), 'secure': bool(c.get('secure')),
             'expires': c.get('expires', -1)}
            for c in cookies
            if c.get('domain', '').lstrip('.') == 'sohu.com'
            or c.get('domain', '').lstrip('.').endswith('.sohu.com')
        ]
        session = _make_session(records)
        save_session(self.destination, session, user_agent=USER_AGENT,
                     network='direct', account_id=self.account_id,
                     dv_id=dv, sp_cm=sp_cm)

    def run(self, *, headless: bool = True) -> dict:
        """执行登录流程，核验通过后才替换正式凭证。"""
        from mulpubcli.browser import PlaywrightLoginer
        target = self.destination
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix='.sohu-login-', suffix='.json', dir=target.parent)
        os.close(fd)
        staged = Path(name)
        staged.unlink()
        self.destination = staged
        try:
            loginer = PlaywrightLoginer(user_agent=USER_AGENT,
                                        storage_state_dir=target.parent)
            result = loginer.login(LOGIN_URL, self._perform, headless=headless,
                                   wait_after_auto_ms=0)
            if result.status != 'ok':
                return {'status': result.status, 'platform': 'sohu',
                        'message': result.message}
            if not staged.is_file():
                return {'status': 'failed', 'platform': 'sohu',
                        'message': '浏览器未导出搜狐登录凭证'}
            try:
                client = SohuWeb.load(staged)
                try:
                    info = client.account()
                    client.save(staged)
                finally:
                    client.close()
            except HTTPFailure as exc:
                return {'status': 'failed', 'platform': 'sohu',
                        'message': f'搜狐登录凭证核验失败：{exc}'}
            os.replace(staged, target)
            self.account_id = info['id']
            return {'status': 'ok', 'platform': 'sohu',
                    'account_id': info['id'], 'username': info.get('name', ''),
                    'account_details': info.get('account_details', {}),
                    'message': '搜狐浏览器登录成功，登录态已生效'}
        finally:
            self.destination = target
            staged.unlink(missing_ok=True)


def _make_session(records):
    """用记录的 Cookie 构造一个 requests.Session，供 save_session 序列化。"""
    import requests
    session = requests.Session()
    from mulpubcli.http import add_cookies
    add_cookies(session, records)
    return session
