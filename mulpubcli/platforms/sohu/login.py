"""搜狐号浏览器登录：填账号密码，必要时完成短信验证，再核验并保存凭证。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import select
import sys
import tempfile
import termios
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .client import SohuWeb, MP, USER_AGENT
from mulpubcli.http import HTTPFailure, save_session


LOGIN_URL = 'https://v4.passport.sohu.com/fe/login?appid=999801&p=password'
EDITOR_URL = MP + '/mpfe/v4/contentManagement/news/addarticle?contentStatus=2'


def _read_tty_code_until(prompt: str, deadline: float) -> str:
    """Read one hidden line without blocking past SMS expiry."""
    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    hidden = termios.tcgetattr(fd)
    hidden[3] &= ~termios.ECHO
    termios.tcsetattr(fd, termios.TCSADRAIN, hidden)
    print(prompt, end='', file=sys.stderr, flush=True)
    try:
        remaining = max(0.0, deadline - time.monotonic())
        if not select.select([fd], [], [], remaining)[0]:
            termios.tcflush(fd, termios.TCIFLUSH)
            return ''
        code = sys.stdin.readline().strip()
        return code if time.monotonic() < deadline else ''
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)
        print(file=sys.stderr, flush=True)


def _sms_valid_seconds(payload: dict, fallback: int) -> int:
    """Use an explicit server TTL when supplied; otherwise use the local cap."""
    sources = [payload, payload.get('data')]
    duration_keys = ('expiresIn', 'expireIn', 'ttlSeconds', 'validSeconds',
                     'expireSeconds', 'codeExpireSeconds', 'smsExpireSeconds')
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in duration_keys:
            try:
                value = float(source[key])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= value <= 1800:
                return int(value)
    return fallback


class SohuLogin:
    """驱动一次搜狐登录并导出凭证到持久化文件。"""

    def __init__(self, phone: str, password: str, destination: Path,
                 *, account_id: str = '', on_challenge=None, mirror_port: int = 0,
                 sms_code_provider: Callable[[int], str] | None = None,
                 cancel_event: threading.Event | None = None,
                 bootstrap_existing: bool = True, fresh_browser: bool = False,
                 allow_human: bool = True):
        if not isinstance(phone, str) or not isinstance(password, str):
            raise ValueError('搜狐手机号与密码须为字符串')
        if '\r' in phone or '\n' in phone or '\r' in password or '\n' in password:
            raise ValueError('搜狐手机号或密码包含非法字符')
        self.phone = phone
        self.password = password
        self.destination = destination
        self.account_id = account_id
        self.dv_id = ''
        self._headless = True
        self.on_challenge = on_challenge
        self.mirror_port = mirror_port
        self.sms_code_provider = sms_code_provider
        self.cancel_event = cancel_event
        self.bootstrap_existing = bootstrap_existing
        self.fresh_browser = fresh_browser
        self.allow_human = allow_human
        self._bootstrap_cookies: list[dict] = []
        self._bootstrap_dv_id = ''

    @staticmethod
    def _profile_name(phone: str) -> str:
        """Keep one browser device per phone without exposing the number in its path."""
        digest = hashlib.sha256(('sohu:' + phone).encode('utf-8')).hexdigest()
        return 'sohu-' + digest[:20]

    @staticmethod
    def _bootstrap_from_existing(path: Path, expected_id: str) -> tuple[list[dict], str]:
        """Only carry current account's unexpired browser state into a new profile."""
        if not expected_id or path.is_symlink() or not path.is_file():
            return [], ''
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return [], ''
        if not isinstance(data, dict) or str(data.get('account_id', '')) != str(expected_id):
            return [], ''
        cookies = []
        now = time.time()
        for item in data.get('cookies', []):
            if not isinstance(item, dict):
                continue
            name, value, domain = item.get('name'), item.get('value'), item.get('domain')
            if not all(isinstance(part, str) and part for part in (name, value, domain)):
                continue
            if domain.lstrip('.') != 'sohu.com' and not domain.lstrip('.').endswith('.sohu.com'):
                continue
            cookie = {'name': name, 'value': value, 'domain': domain,
                      'path': item.get('path') or '/', 'secure': bool(item.get('secure'))}
            expiry_value = item.get('expires')
            if expiry_value is not None:
                try:
                    expiry = float(expiry_value)
                except (TypeError, ValueError):
                    continue
                if expiry >= 0:
                    if expiry <= now:
                        continue
                    cookie['expires'] = expiry
            cookies.append(cookie)
        dv_id = data.get('dv_id', '')
        return cookies, dv_id if isinstance(dv_id, str) else ''

    def _mirror_ready(self, url: str) -> None:
        """Expose an embeddable URL without handing out cookies or a second session."""
        if self.on_challenge is not None:
            self.on_challenge(url)
            return
        print(f'[搜狐] 请打开窗口并通过临时滑块窗口：[{url}]({url})',
              file=sys.stderr, flush=True)

    def _solve_sms_code(self, page, code_timeout_s: int | None = None) -> bool:
        """等用户输入短信验证码并提交。找不到输入框则跳过。

        优先从终端限时读取；非 tty / 无人值守（由外部网页服务或上层托管）时，
        轮询临时文件 code_file（换行分隔每位或整串）读取。——文件由调用方或用户写入。
        """
        code_input = page.locator("input[placeholder='请输入短信验证码']")
        if code_input.count() != 1:
            raise HTTPFailure('搜狐短信授权页没有验证码输入框', kind='verification_required')
        # 只有发送接口明确接受请求，才提示用户输入短信。
        send_button = page.get_by_role('button', name='获取验证码', exact=True)
        from playwright.sync_api import TimeoutError as PWTimeout
        try:
            with page.expect_response(
                    lambda response: '/account/cv/send-sms-v2' in response.url,
                    timeout=15_000) as sent:
                send_button.click(timeout=5000)
            response = sent.value
        except PWTimeout:
            raise HTTPFailure('搜狐未确认短信验证码发送，登录已停止',
                              kind='verification_required') from None
        try:
            payload = response.json()
        except ValueError:
            raise HTTPFailure('搜狐短信发送接口未返回有效结果',
                              kind='invalid_response') from None
        code = payload.get('code') if isinstance(payload, dict) else None
        if code == 9000000:
            if self._headless:
                from mulpubcli.browser_mirror import BrowserMirror
                with BrowserMirror(
                        port=self.mirror_port,
                        cancel_event=self.cancel_event) as mirror:
                    response = mirror.wait_for_sms_send(
                        page, on_ready=self._mirror_ready)
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise HTTPFailure('搜狐登录已取消', kind='verification_required')
            else:
                print('[搜狐] 页面要求验证码；请在浏览器里完成验证，必要时再次点击获取验证码。',
                      file=sys.stderr, flush=True)
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    remaining = max(1, int((deadline - time.monotonic()) * 1000))
                    try:
                        response = page.wait_for_event(
                            'response',
                            predicate=lambda item: '/account/cv/send-sms-v2' in item.url,
                            timeout=min(10_000, remaining))
                    except PWTimeout:
                        continue
                    try:
                        payload = response.json()
                    except ValueError:
                        continue
                    code = payload.get('code') if isinstance(payload, dict) else None
                    if response.ok and code in (0, 200, 2000000):
                        break
                    if code != 9000000:
                        raise HTTPFailure(f'搜狐短信发送未成功（code={code}），登录已停止',
                                          kind='verification_required')
                else:
                    raise HTTPFailure('等待搜狐页面验证与短信发送超时，登录未完成',
                                      kind='verification_required')
            payload = response.json()
            code = payload.get('code') if isinstance(payload, dict) else None
        if not response.ok or code not in (0, 200, 2000000):
            raise HTTPFailure(f'搜狐短信发送未成功（code={code}），登录已停止',
                              kind='verification_required')
        if code_timeout_s is None:
            try:
                code_timeout_s = int(os.environ.get('SOHU_SMS_CODE_TTL_SECONDS', '120'))
            except ValueError:
                code_timeout_s = 120
        code_timeout_s = _sms_valid_seconds(
            payload, min(max(0, code_timeout_s), 1800))
        deadline = time.monotonic() + code_timeout_s
        sent_at_wall = time.time()
        print(f'[搜狐] 短信发送接口已确认，等待验证码（最多 {code_timeout_s} 秒）…',
              file=sys.stderr, flush=True)
        # 2) 限时读取短信验证码；受托管的 edge 页面直接等待其一次性输入。
        code_file = Path(self.destination.parent) / '.dev' / 'sohu_sms_code.txt'
        code = ''
        if self.sms_code_provider is not None:
            code = self.sms_code_provider(max(0, int(deadline - time.monotonic())))
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise HTTPFailure('搜狐登录已取消', kind='verification_required')
        if self.sms_code_provider is None and sys.stdin.isatty():
            try:
                code = _read_tty_code_until(
                    '请输入搜狐短信验证码（6 位，不回显）: ', deadline)
            except (EOFError, OSError, termios.error):
                code = ''
        if self.sms_code_provider is None and not code and time.monotonic() < deadline:
            code_file.parent.mkdir(parents=True, exist_ok=True)
            print(f'[搜狐] 等待短信验证码… 将 6 位验证码写入 {code_file}',
                  file=sys.stderr, flush=True)
            last = None
            while time.monotonic() < deadline and not code:
                try:
                    if code_file.exists() and code_file.stat().st_mtime >= sent_at_wall - 1:
                        cur = code_file.read_text(encoding='utf-8').strip()
                    else:
                        cur = ''
                except Exception:
                    cur = ''
                if cur and cur != last:
                    code = ''.join(ch for ch in cur if ch.isdigit())
                last = cur
                time.sleep(min(1, max(0, deadline - time.monotonic())))
            if code and code_file.exists():
                try:
                    code_file.unlink()
                except Exception:
                    pass
        if not code:
            raise HTTPFailure('搜狐短信验证码等待到期，登录已结束',
                              kind='verification_required')
        if not re.fullmatch(r'\d{6}', code):
            raise HTTPFailure('搜狐短信验证码须为 6 位数字', kind='verification_required')
        try:
            code_input.fill(code)
        except Exception:
            return False
        # 只点击短信授权表单的提交按钮；未提交不能报告成功。
        try:
            page.get_by_role('button', name='提交', exact=True).click(timeout=5000)
        except PWTimeout:
            raise HTTPFailure('搜狐短信验证码提交按钮不可用',
                              kind='verification_required') from None
        try:
            page.wait_for_function(
                "() => !location.href.includes('/clientAuth') || "
                "!document.body.innerText.includes('短信验证')",
                timeout=15_000)
        except Exception:
            pass
        return True

    @staticmethod
    def _wait_for_editor_or_auth(page, *, allow_login: bool = False) -> None:
        """等后台编辑器或设备授权页实际出现，再判断下一步。"""
        condition = (
            "location.pathname.includes('/clientAuth') || "
            "(location.hostname === 'mp.sohu.com' && "
            "location.pathname.includes('/contentManagement/') && "
            "(document.querySelector('[contenteditable=true]') || "
            "document.querySelector('input[placeholder*=标题]')))"
        )
        if allow_login:
            condition += (
                " || (location.hostname.includes('passport.sohu.com') && document.querySelector('.login-button'))"
                " || (location.hostname === 'mp.sohu.com' && location.pathname === '/')")
        page.wait_for_function('() => ' + condition, timeout=20_000)

    def _perform(self, page, browser, ctx):
        """执行登录自动化。返回 True 表示仍需外部帮助，False 表示已全部完成。"""
        from playwright.sync_api import TimeoutError as PWTimeout
        # 已验证过的浏览器先检查编辑页，避免强制刷新或自动续期时重复密码登录。
        if self._bootstrap_cookies:
            if self._bootstrap_dv_id:
                dv = json.dumps(self._bootstrap_dv_id)
                page.add_init_script(
                    "if (location.hostname === 'mp.sohu.com' && "
                    "!localStorage.getItem('preview-dv-id')) "
                    f"localStorage.setItem('preview-dv-id', {dv})")
            page.context.add_cookies(self._bootstrap_cookies)
        browser_authorized = any(
            cookie.get('name') == 'mp-cv' and cookie.get('value')
            for cookie in page.context.cookies())
        if browser_authorized:
            try:
                page.goto(EDITOR_URL, timeout=50000, wait_until='domcontentloaded')
                self._wait_for_editor_or_auth(page, allow_login=True)
            except PWTimeout:
                raise HTTPFailure('搜狐文章编辑页或登录页未就绪，登录未完成',
                                  kind='verification_required') from None
            location = urlsplit(page.url or '')
            browser_authorized = (location.hostname == 'mp.sohu.com' and
                                  ('/contentManagement/' in location.path or
                                   '/clientAuth' in location.path))

        if not browser_authorized:
            if 'passport.sohu.com' not in (page.url or ''):
                page.goto(LOGIN_URL, timeout=45000, wait_until='domcontentloaded')
            page.fill("input[type=text]:nth-of-type(1)", self.phone)
            page.fill("input[type=password]:nth-of-type(1)", self.password)
            button = page.locator(".login-button").first
            button.wait_for(state='visible', timeout=8000)
            button.click(timeout=8000)
            try:
                page.wait_for_function(
                    "() => !location.href.includes('/fe/login') || "
                    "!document.querySelector('.login-button')",
                    timeout=12_000)
            except PWTimeout:
                pass
            try:
                page.goto(EDITOR_URL, timeout=50000, wait_until='domcontentloaded')
                self._wait_for_editor_or_auth(page)
            except PWTimeout:
                raise HTTPFailure('搜狐文章编辑页或短信授权页未就绪，登录未完成',
                                  kind='verification_required') from None

        # 判断是否需真人授权：URL 停在 clientAuth 或页面含"短信验证"字样。
        try:
            body_text = page.evaluate("document.body.innerText")
        except Exception:
            body_text = ''
        need_human = '/clientAuth' in (page.url or '') or '短信验证' in body_text
        self._need_human = need_human

        # 第二步：搜狐要求设备授权时，由终端输入短信验证码。
        if need_human:
            if not self.allow_human:
                raise HTTPFailure('搜狐要求短信授权，自动刷新已停止；旧凭证未替换，请通过交互式登录完成验证',
                                  kind='verification_required')
            if not self._solve_sms_code(page):
                raise HTTPFailure('搜狐需要短信授权，但验证码未完成',
                                  kind='verification_required')
            # 短信通过后，回到编辑器；已授权设备不会再被拦。
            try:
                page.goto(EDITOR_URL, timeout=50000, wait_until='domcontentloaded')
                self._wait_for_editor_or_auth(page)
            except PWTimeout:
                raise HTTPFailure('搜狐短信提交后文章编辑页未就绪',
                                  kind='verification_required') from None
            if '/clientAuth' in (page.url or ''):
                raise HTTPFailure('搜狐短信授权未通过，登录未完成',
                                  kind='verification_required')

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
        self._headless = headless
        self._bootstrap_cookies, self._bootstrap_dv_id = (
            self._bootstrap_from_existing(target, self.account_id)
            if self.bootstrap_existing and not self.fresh_browser else ([], ''))
        try:
            loginer = PlaywrightLoginer(user_agent=USER_AGENT,
                                        storage_state_dir=target.parent,
                                        profile_name=(None if self.fresh_browser else
                                                      self._profile_name(self.phone)))
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
                    if self.account_id and str(info['id']) != str(self.account_id):
                        return {'status': 'failed', 'platform': 'sohu',
                                'message': '浏览器登录的搜狐账号与原账号不一致，未替换凭证'}
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
