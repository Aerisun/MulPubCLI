"""Tests for Zhihu browser QR login and verified cookie export.

覆盖：浏览器上下文 Cookie 导出为正式凭证（知乎域名过滤、z_c0 要求、账号核验后
原子落盘）、perform 已登录/需扫码判定与二维码输出、human_done 页面事件判定，
以及 CLI 扫码通知与登录结果接线。
"""
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image

from mulpubcli.storage import StorageLayout
from mulpubcli.platforms.zhihu.browser_login import ZhihuBrowserLogin
from mulpubcli.browser import LoginResult
from mulpubcli.http import HTTPFailure

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _login_args(proxy=None):
    return SimpleNamespace(platform='zhihu', proxy=proxy)


class _FakePage:
    """Playwright page 替身：可记录等待，并挂一个 context。"""

    def __init__(self, context=None):
        self.wait_calls = 0
        self.context = context

    def wait_for_selector(self, *args, **kwargs):
        raise AssertionError('已有登录态无需等待二维码画布')

    def wait_for_timeout(self, ms):
        self.wait_calls += 1

    def screenshot(self, path=None, **kwargs):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b'png')


def _qr_value(token='test-token'):
    return f'https://www.zhihu.com/account/scan/login/{token}'


class _CanvasPage:
    def __init__(self, value):
        self.value = value
        self.context = _FakeCtx(*_cookies(with_login=False))
        self.wait_calls = 0
        self.selector_waits = []

    def wait_for_timeout(self, ms):
        self.wait_calls += 1

    def wait_for_selector(self, selector, **kwargs):
        self.selector_waits.append((selector, kwargs))
        return self.query_selector(selector)

    def query_selector(self, selector):
        if selector == 'canvas.Qrcode-qrcode':
            def extract(script):
                if 'toDataURL' in script:
                    raise AssertionError('跨域画布不能导出为 PNG')
                self.assert_react_probe(script)
                return self.value
            return SimpleNamespace(evaluate=extract)
        return None

    @staticmethod
    def assert_react_probe(script):
        if 'memoizedProps' not in script:
            raise AssertionError('应读取二维码原始内容，而非画布像素')

    def screenshot(self, *args, **kwargs):
        raise AssertionError('二维码不能退回页面截图')


def _cookies(with_login=True):
    out = [
        {'name': 'd_c0', 'value': 'dev-token', 'domain': '.zhihu.com', 'path': '/',
         'expires': -1.0, 'secure': False, 'httpOnly': False, 'sameSite': 'Lax',
         'hostOnly': False},
        {'name': 'google_g', 'value': 'other', 'domain': '.google.com', 'path': '/',
         'expires': -1.0, 'secure': False, 'httpOnly': False, 'sameSite': 'Lax',
         'hostOnly': False},
    ]
    if with_login:
        out.insert(0, {'name': 'z_c0', 'value': 'browser-zc0', 'domain': '.zhihu.com',
                       'path': '/', 'expires': -1.0, 'secure': True, 'httpOnly': True,
                       'sameSite': 'Lax', 'hostOnly': False})
    return out


class _FakeCtx:
    def __init__(self, *cookies):
        self._cookies = cookies

    def cookies(self):
        return list(self._cookies)


class ExportTests(unittest.TestCase):
    def test_export_saves_verified_credential_and_filters_domains(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            dest = store.credentials('zhihu')
            loginer = ZhihuBrowserLogin(dest)
            ctx = _FakeCtx(*_cookies())

            def _fake_account(self):
                self.account_id = 'user-browser'
                return {'id': 'user-browser', 'name': '扫码账号'}

            with patch('mulpubcli.platforms.zhihu.client.ZhihuWeb.account', _fake_account):
                info = loginer._export(ctx)
            self.assertEqual(info['id'], 'user-browser')
            self.assertEqual(loginer.account_id, 'user-browser')
            self.assertTrue(dest.is_file())
            data = json.loads(dest.read_text(encoding='utf-8'))
            self.assertEqual(data['account_id'], 'user-browser')
            names = {c['name'] for c in data['cookies']}
            self.assertIn('z_c0', names)
            self.assertIn('d_c0', names)
            self.assertNotIn('google_g', names)  # 非知乎域名被过滤
            self.assertIs(data.get('login_blocked'), False)


class PerformTests(unittest.TestCase):
    def test_perform_exports_when_login_cookie_present(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            dest = store.credentials('zhihu')
            qr = store.qr_image('zhihu')
            loginer = ZhihuBrowserLogin(dest, qr_path=qr)
            page = _FakePage()
            ctx = _FakeCtx(*_cookies(with_login=True))
            exported = []
            with patch.object(ZhihuBrowserLogin, '_export',
                              side_effect=lambda c: exported.append(True)):
                done = loginer._perform(page, None, ctx)
            self.assertIs(done, False)         # 已登录，无需真人
            self.assertEqual(len(exported), 1)
            self.assertEqual(page.wait_calls, 0)

    def test_perform_requests_human_and_snapshots_when_no_login(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            dest = store.credentials('zhihu')
            qr = store.qr_image('zhihu')
            loginer = ZhihuBrowserLogin(dest, qr_path=qr)
            page = _CanvasPage(_qr_value())
            ctx = _FakeCtx(*_cookies(with_login=False))
            with patch.object(ZhihuBrowserLogin, '_export') as export:
                done = loginer._perform(page, None, ctx)
            self.assertIs(done, True)          # 等待用户扫码
            export.assert_not_called()
            self.assertTrue(qr.is_file())
            with Image.open(qr) as image:
                self.assertGreaterEqual(image.width, 1000)
                self.assertEqual(image.width, image.height)
                self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
            self.assertEqual(qr.stat().st_mode & 0o777, 0o600)
            self.assertEqual(page.wait_calls, 0)
            self.assertEqual(page.selector_waits[0][0], 'canvas.Qrcode-qrcode')
            self.assertEqual(page.selector_waits[0][1]['state'], 'visible')

    def test_perform_refuses_missing_qr_without_page_screenshot(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            qr = store.qr_image('zhihu')
            loginer = ZhihuBrowserLogin(store.credentials('zhihu'), qr_path=qr)
            page = _CanvasPage(_qr_value())
            page.query_selector = lambda selector: None
            with self.assertRaises(HTTPFailure):
                loginer._perform(page, None, page.context)
            self.assertFalse(qr.exists())

    def test_perform_reports_missing_qr_after_page_wait_times_out(self):
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            qr = store.qr_image('zhihu')
            loginer = ZhihuBrowserLogin(store.credentials('zhihu'), qr_path=qr)
            page = _CanvasPage(_qr_value())
            def missing(*args, **kwargs):
                raise PlaywrightTimeoutError('timeout')
            page.wait_for_selector = missing
            with self.assertRaises(HTTPFailure) as error:
                loginer._perform(page, None, page.context)
            self.assertEqual(error.exception.kind, 'invalid_response')
            self.assertFalse(qr.exists())

    def test_same_canvas_does_not_rewrite_qr_file(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            qr = store.qr_image('zhihu')
            loginer = ZhihuBrowserLogin(store.credentials('zhihu'), qr_path=qr)
            page = _CanvasPage(_qr_value())
            loginer._snapshot(page, required=True)
            os.utime(qr, ns=(1, 1))
            with patch.object(loginer, '_render_qr', side_effect=AssertionError('二维码内容未变')):
                loginer._snapshot(page, required=True)
            self.assertEqual(qr.stat().st_mtime_ns, 1)

    def test_changed_qr_value_replaces_image(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            qr = store.qr_image('zhihu')
            loginer = ZhihuBrowserLogin(store.credentials('zhihu'), qr_path=qr)
            page = _CanvasPage(_qr_value('first-token'))
            loginer._snapshot(page, required=True)
            first = qr.read_bytes()
            page.value = _qr_value('second-token')
            loginer._snapshot(page, required=True)
            self.assertNotEqual(qr.read_bytes(), first)

    def test_rejects_non_zhihu_qr_value(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            qr = store.qr_image('zhihu')
            loginer = ZhihuBrowserLogin(store.credentials('zhihu'), qr_path=qr)
            with self.assertRaises(HTTPFailure):
                loginer._snapshot(_CanvasPage('https://example.com/qr'), required=True)
            self.assertFalse(qr.exists())


class HumanDoneTests(unittest.TestCase):
    def test_wait_for_page_update_listens_to_existing_qr_responses(self):
        login = ZhihuBrowserLogin(Path('/tmp/zhihu-test.json'))
        responses = [
            SimpleNamespace(url='https://www.zhihu.com/api/v3/account/api/login/qrcode',
                            request=SimpleNamespace(method='POST', resource_type='fetch')),
            SimpleNamespace(url='https://www.zhihu.com/api/v3/account/api/login/qrcode/token/scan_info',
                            request=SimpleNamespace(method='GET', resource_type='fetch')),
        ]
        class FakePage:
            def wait_for_event(self, event, *, predicate, timeout):
                self.event, self.timeout = event, timeout
                self.matches = [predicate(response) for response in responses]
                return SimpleNamespace(finished=lambda: None)
            def wait_for_timeout(self, ms):
                self.settle_ms = ms
        page = FakePage()
        login._wait_for_page_update(page, remaining_ms=1_000)
        self.assertEqual(page.event, 'response')
        self.assertEqual(page.matches, [True, True])
        self.assertLessEqual(page.timeout, 1_000)
        self.assertLessEqual(page.settle_ms, 500)

    def test_two_minute_timeout_expires_login_and_removes_qr(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            qr = store.qr_image('zhihu')
            qr.parent.mkdir(parents=True, exist_ok=True)
            qr.write_bytes(b'old qr')
            login = ZhihuBrowserLogin(store.credentials('zhihu'), qr_path=qr,
                                      max_wait_ms=None)
            self.assertEqual(ZhihuBrowserLogin(store.credentials('zhihu')).max_wait_ms,
                             120_000)
            self.assertEqual(ZhihuBrowserLogin(store.credentials('zhihu'),
                                              max_wait_ms=300_000).max_wait_ms, 120_000)
            with patch('mulpubcli.browser.PlaywrightLoginer.login', return_value=LoginResult(
                    status='failed', timed_out=True)) as run:
                result = login.run()
            self.assertEqual(run.call_args.kwargs['max_human_wait_ms'], 120_000)
            self.assertEqual(run.call_args.kwargs['wait_after_auto_ms'], 0)
            self.assertEqual(result['status'], 'expired')
            self.assertIn('2 分钟', result['message'])
            self.assertFalse(qr.exists())

    def test_human_done_exports_once_login_cookie_appears(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            dest = store.credentials('zhihu')
            loginer = ZhihuBrowserLogin(dest)
            page = _FakePage(context=_FakeCtx(*_cookies(with_login=True)))
            exported = []
            with patch.object(ZhihuBrowserLogin, '_export',
                              side_effect=lambda c: exported.append(True)):
                done = loginer._human_done(page)
            self.assertTrue(done)
            self.assertEqual(len(exported), 1)

    def test_human_done_false_before_login(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            dest = store.credentials('zhihu')
            loginer = ZhihuBrowserLogin(dest)
            page = _FakePage(context=_FakeCtx(*_cookies(with_login=False)))
            with patch.object(ZhihuBrowserLogin, '_export') as export:
                done = loginer._human_done(page)
            self.assertFalse(done)
            export.assert_not_called()


class CliBrowserLoginTests(unittest.TestCase):
    def test_browser_login_cli_reports_authenticated(self):
        from mulpubcli.__main__ import _cmd_login
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            with patch('mulpubcli.__main__._out') as out, \
                 patch('mulpubcli.platforms.zhihu.browser_login.ZhihuBrowserLogin.run',
                       return_value={'status': 'ok', 'account_id': 'u-br', 'message': ''}):
                code = _cmd_login(_login_args(), store)
            self.assertEqual(code, 0)
            payload = out.call_args[0][0]
            self.assertEqual(payload['status'], 'authenticated')
            self.assertEqual(payload['account_id'], 'u-br')


if __name__ == '__main__':
    unittest.main()
