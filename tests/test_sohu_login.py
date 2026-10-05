"""搜狐登录须核验账号，并复用已验证的凭证。"""
import json
import os
import termios
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from mulpubcli.__main__ import _sohu_login, _build_parser
from mulpubcli.browser import LoginResult
from mulpubcli.http import HTTPFailure
from mulpubcli.platforms.sohu.client import SohuWeb
from mulpubcli.platforms.sohu.login import (
    SohuLogin, _read_tty_code_until, _sms_valid_seconds)
from mulpubcli.storage import StorageLayout
from playwright.sync_api import TimeoutError as PlaywrightTimeout


class SohuLoginTests(unittest.TestCase):
    def test_browser_profile_is_stable_and_account_scoped(self):
        first = SohuLogin._profile_name('13522877362')
        self.assertEqual(first, SohuLogin._profile_name('13522877362'))
        self.assertNotEqual(first, SohuLogin._profile_name('13992649062'))
        self.assertNotIn('13522877362', first)

    def test_saved_state_bootstrap_requires_same_account_and_unexpired_cookie(self):
        with TemporaryDirectory() as root:
            path = Path(root) / 'sohu.json'
            path.write_text(json.dumps({
                'account_id': 'expected', 'dv_id': 'device',
                'cookies': [
                    {'name': 'mp-cv', 'value': 'active', 'domain': 'mp.sohu.com',
                     'path': '/', 'secure': True, 'expires': time.time() + 600},
                    {'name': 'ppinf', 'value': 'session', 'domain': '.sohu.com',
                     'path': '/', 'expires': None},
                    {'name': 'old', 'value': 'expired', 'domain': '.sohu.com',
                     'path': '/', 'expires': time.time() - 60},
                ],
            }), encoding='utf-8')
            cookies, dv = SohuLogin._bootstrap_from_existing(path, 'expected')
            self.assertEqual([cookie['name'] for cookie in cookies], ['mp-cv', 'ppinf'])
            self.assertNotIn('expires', cookies[1])
            self.assertEqual(dv, 'device')
            self.assertEqual(SohuLogin._bootstrap_from_existing(path, 'other'), ([], ''))

    def test_existing_browser_authorization_skips_password_login(self):
        login = SohuLogin('phone', 'password', Path('/tmp/unused-sohu.json'))
        page = Mock()
        page.context.cookies.return_value = [{'name': 'mp-cv', 'value': 'valid'}]
        page.url = 'https://mp.sohu.com/mpfe/v4/contentManagement/news/addarticle'
        page.evaluate.return_value = ''
        with patch.object(login, '_export') as export:
            login._perform(page, None, None)
        page.fill.assert_not_called()
        export.assert_called_once_with(page)

    def test_existing_browser_cookie_still_restores_session_cookies(self):
        login = SohuLogin('phone', 'password', Path('/tmp/unused-sohu.json'))
        login._bootstrap_cookies = [{'name': 'ppinf', 'value': 'session',
                                     'domain': '.sohu.com', 'path': '/'}]
        page = Mock()
        page.context.cookies.return_value = [{'name': 'mp-cv', 'value': 'valid'}]
        page.url = 'https://mp.sohu.com/mpfe/v4/contentManagement/news/addarticle'
        page.evaluate.return_value = ''
        with patch.object(login, '_export'):
            login._perform(page, None, None)
        page.context.add_cookies.assert_called_once_with(login._bootstrap_cookies)
        page.fill.assert_not_called()

    def test_browser_redirect_to_public_home_falls_back_to_password_login(self):
        login = SohuLogin('phone', 'password', Path('/tmp/unused-sohu.json'))
        page = Mock()
        page.context.cookies.return_value = [{'name': 'mp-cv', 'value': 'old'}]
        page.url = 'https://v4.passport.sohu.com/fe/login'
        page.evaluate.return_value = ''
        urls = iter(['https://mp.sohu.com/',
                     'https://v4.passport.sohu.com/fe/login',
                     'https://mp.sohu.com/mpfe/v4/contentManagement/news/addarticle'])
        page.goto.side_effect = lambda *_args, **_kwargs: setattr(page, 'url', next(urls))
        with patch.object(login, '_export'):
            login._perform(page, None, None)
        page.fill.assert_any_call('input[type=text]:nth-of-type(1)', 'phone')

    def test_sms_validity_uses_server_ttl_or_two_minute_fallback(self):
        self.assertEqual(_sms_valid_seconds({'data': {'expiresIn': 90}}, 120), 90)
        self.assertEqual(_sms_valid_seconds({'data': {}}, 120), 120)

    def test_sms_tty_input_times_out_and_restores_echo(self):
        master, slave = os.openpty()
        with os.fdopen(slave, 'r') as stream:
            original = termios.tcgetattr(stream.fileno())
            try:
                with patch('sys.stdin', stream):
                    code = _read_tty_code_until('验证码: ', time.monotonic() + .03)
                self.assertEqual(code, '')
                self.assertEqual(termios.tcgetattr(stream.fileno()), original)
            finally:
                os.close(master)

    def test_sms_prompt_requires_confirmed_send_response(self):
        with TemporaryDirectory() as root:
            login = SohuLogin('phone', 'password', Path(root) / 'sohu.json')
            page = Mock()
            page.locator.return_value.last.count.return_value = 1
            page.expect_response.side_effect = PlaywrightTimeout('send timeout')
            with patch('sys.stdin.isatty', return_value=False):
                with self.assertRaises(HTTPFailure):
                    login._solve_sms_code(page, code_timeout_s=0)

    def test_sms_challenge_reports_unsent_without_exposing_challenge_data(self):
        with TemporaryDirectory() as root:
            login = SohuLogin('phone', 'password', Path(root) / 'sohu.json')
            page = Mock()
            page.locator.return_value.count.return_value = 1
            response = Mock(ok=True)
            response.json.return_value = {'code': 9000000, 'msg': 'opaque-challenge-token'}
            page.expect_response.return_value = nullcontext(SimpleNamespace(value=response))
            mirror = MagicMock()
            mirror.__enter__.return_value = mirror
            mirror.wait_for_sms_send.side_effect = HTTPFailure('页面验证码超时')
            with patch('mulpubcli.browser_mirror.BrowserMirror', return_value=mirror):
                with self.assertRaisesRegex(HTTPFailure, '页面验证码') as failure:
                    login._solve_sms_code(page)
            self.assertNotIn('opaque-challenge-token', str(failure.exception))

    def test_visible_browser_waits_for_challenge_then_sms_send(self):
        with TemporaryDirectory() as root:
            login = SohuLogin('phone', 'password', Path(root) / 'sohu.json')
            login._headless = False
            page = Mock()
            page.locator.return_value.count.return_value = 1
            challenge = Mock(ok=True)
            challenge.json.return_value = {'code': 9000000, 'msg': 'opaque'}
            accepted = Mock(ok=True)
            accepted.json.return_value = {'code': 2000000}
            page.expect_response.return_value = nullcontext(SimpleNamespace(value=challenge))
            page.wait_for_event.return_value = accepted
            with patch('sys.stdin.isatty', return_value=True), \
                 patch('mulpubcli.platforms.sohu.login._read_tty_code_until',
                       return_value='123456'):
                self.assertTrue(login._solve_sms_code(page))
            page.wait_for_event.assert_called_once()
            page.get_by_role.assert_any_call('button', name='提交', exact=True)

    def test_headless_challenge_uses_temporary_browser_mirror(self):
        with TemporaryDirectory() as root:
            on_challenge = Mock()
            login = SohuLogin('phone', 'password', Path(root) / 'sohu.json',
                              on_challenge=on_challenge)
            page = Mock()
            page.locator.return_value.count.return_value = 1
            challenge = Mock(ok=True)
            challenge.json.return_value = {'code': 9000000, 'msg': 'opaque'}
            accepted = Mock(ok=True)
            accepted.json.return_value = {'code': 2000000}
            page.expect_response.return_value = nullcontext(SimpleNamespace(value=challenge))
            mirror = MagicMock()
            mirror.__enter__.return_value = mirror
            mirror.url = 'http://127.0.0.1:8765/t/test/'
            mirror.wait_for_sms_send.side_effect = lambda page, on_ready: (
                on_ready(mirror.url), accepted)[1]
            with patch('mulpubcli.browser_mirror.BrowserMirror', return_value=mirror), \
                 patch('sys.stdin.isatty', return_value=True), \
                 patch('mulpubcli.platforms.sohu.login._read_tty_code_until',
                       return_value='123456'):
                self.assertTrue(login._solve_sms_code(page))
            mirror.wait_for_sms_send.assert_called_once()
            on_challenge.assert_called_once_with(mirror.url)

    def test_cancelling_sms_prompt_stops_login_immediately(self):
        with TemporaryDirectory() as root:
            login = SohuLogin('phone', 'password', Path(root) / 'sohu.json')
            page = Mock()
            page.locator.return_value.count.return_value = 1
            accepted = Mock(ok=True)
            accepted.json.return_value = {'code': 2000000}
            page.expect_response.return_value = nullcontext(SimpleNamespace(value=accepted))
            with patch('sys.stdin.isatty', return_value=True), \
                 patch('mulpubcli.platforms.sohu.login._read_tty_code_until',
                       side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    login._solve_sms_code(page, code_timeout_s=0)

    def test_fast_path_does_not_add_long_fixed_sleeps(self):
        login = SohuLogin('phone', 'password', Path('/tmp/unused-sohu.json'))
        page = Mock()
        page.context.cookies.return_value = []
        page.url = 'https://mp.sohu.com/mpfe/v4/contentManagement/news/addarticle'
        page.evaluate.return_value = ''
        with patch.object(login, '_export'):
            login._perform(page, None, None)
        waits = [call.args[0] for call in page.wait_for_timeout.call_args_list]
        self.assertFalse(any(wait >= 4_000 for wait in waits))
        self.assertGreaterEqual(page.wait_for_function.call_count, 2)

    def test_sms_challenge_cannot_be_reported_as_authenticated_when_unsolved(self):
        login = SohuLogin('phone', 'password', Path('/tmp/unused-sohu.json'))
        page = Mock()
        page.context.cookies.return_value = []
        page.url = 'https://mp.sohu.com/clientAuth'
        page.evaluate.return_value = '短信验证'
        with patch.object(login, '_solve_sms_code', return_value=False), \
             patch.object(login, '_export') as export:
            with self.assertRaises(HTTPFailure):
                login._perform(page, None, None)
        export.assert_not_called()

    def test_automatic_refresh_does_not_send_sms_or_prompt_for_code(self):
        login = SohuLogin('phone', 'password', Path('/tmp/unused-sohu.json'),
                          allow_human=False)
        page = Mock()
        page.context.cookies.return_value = [{'name': 'mp-cv', 'value': 'valid'}]
        page.url = 'https://mp.sohu.com/clientAuth'
        page.evaluate.return_value = '短信验证'
        with patch.object(login, '_solve_sms_code') as send_sms, \
             patch.object(login, '_export') as export:
            with self.assertRaises(HTTPFailure) as failure:
                login._perform(page, None, None)
        self.assertEqual(failure.exception.kind, 'verification_required')
        send_sms.assert_not_called()
        export.assert_not_called()

    def test_waits_for_editor_application_before_deciding_no_sms(self):
        login = SohuLogin('phone', 'password', Path('/tmp/unused-sohu.json'))
        page = Mock()
        page.context.cookies.return_value = []
        page.url = 'https://mp.sohu.com/mpfe/v4/contentManagement/news/addarticle'
        page.evaluate.side_effect = lambda script: '短信验证' if '/clientAuth' in page.url else ''

        editor_checks = 0

        def wait_for_page(predicate, **kwargs):
            nonlocal editor_checks
            if 'contenteditable' in predicate:
                editor_checks += 1
                page.url = ('https://mp.sohu.com/mpfe/v4/clientAuth'
                            if editor_checks == 1 else
                            'https://mp.sohu.com/mpfe/v4/contentManagement/news/addarticle')

        page.wait_for_function.side_effect = wait_for_page
        with patch.object(login, '_solve_sms_code', return_value=True) as sms, \
             patch.object(login, '_export'):
            login._perform(page, None, None)
        sms.assert_called_once_with(page)

    def test_account_discovers_single_verified_identity(self):
        client = SohuWeb(account_id='')
        response = Mock()
        response.json.return_value = {
            'code': 2000000,
            'data': {'data': [{'accounts': [{'id': 123, 'nickName': 'Alice',
                                            'accountTypeName': '个人',
                                            'statusName': '账号新手期',
                                            'homePage': 'https://mp.sohu.com/profile?id=123'}]}]},
        }
        client.http.request = Mock(return_value=response)
        try:
            info = client.account()
        finally:
            client.close()
        self.assertEqual(info, {'id': '123', 'name': 'Alice', 'account_details': {
            'account_type': '个人', 'account_status': '账号新手期',
            'homepage': 'https://mp.sohu.com/profile?id=123',
        }})
        self.assertEqual(client.account_id, '123')

    def test_export_preserves_browser_cookie_expiry(self):
        with TemporaryDirectory() as root:
            target = Path(root) / 'sohu.json'
            login = SohuLogin('phone', 'password', target)
            page = Mock()
            page.context.cookies.return_value = [
                {'name': 't', 'value': 'session', 'domain': '.sohu.com',
                 'path': '/', 'secure': True, 'expires': 1_800_000_000},
            ]
            page.evaluate.return_value = ''
            login._export(page)
            cookies = json.loads(target.read_text(encoding='utf-8'))['cookies']
            self.assertEqual(cookies[0]['expires'], 1_800_000_000)
            self.assertTrue(cookies[0]['secure'])

    def test_failed_verification_preserves_existing_credential(self):
        with TemporaryDirectory() as root:
            target = Path(root) / 'sohu.json'
            target.write_text('existing', encoding='utf-8')
            login = SohuLogin('phone', 'password', target)

            def browser_login(*args, **kwargs):
                login.destination.write_text('unverified', encoding='utf-8')
                return LoginResult(status='ok')

            with patch('mulpubcli.browser.PlaywrightLoginer') as browser, \
                 patch('mulpubcli.platforms.sohu.client.SohuWeb.load',
                       side_effect=HTTPFailure('not logged in', kind='authentication_required')):
                browser.return_value.login.side_effect = browser_login
                result = login.run()

            self.assertEqual(result['status'], 'failed')
            self.assertEqual(target.read_text(encoding='utf-8'), 'existing')
            self.assertEqual(login.destination, target)
            self.assertEqual(list(Path(root).glob('.sohu-login-*')), [])

    def test_verified_login_returns_identity_and_replaces_credential(self):
        with TemporaryDirectory() as root:
            target = Path(root) / 'sohu.json'
            target.write_text('existing', encoding='utf-8')
            login = SohuLogin('phone', 'password', target)

            def browser_login(*args, **kwargs):
                login.destination.write_text('exported', encoding='utf-8')
                return LoginResult(status='ok')

            client = Mock()
            client.account.return_value = {'id': '123', 'name': 'Alice',
                                           'account_details': {'account_status': '正常'}}
            client.save.side_effect = lambda path: path.write_text('verified', encoding='utf-8')
            with patch('mulpubcli.browser.PlaywrightLoginer') as browser, \
                 patch('mulpubcli.platforms.sohu.client.SohuWeb.load', return_value=client):
                browser.return_value.login.side_effect = browser_login
                result = login.run()

            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['account_id'], '123')
            self.assertEqual(result['username'], 'Alice')
            self.assertEqual(result['account_details']['account_status'], '正常')
            self.assertEqual(target.read_text(encoding='utf-8'), 'verified')
            client.close.assert_called_once()
            self.assertEqual(browser.return_value.login.call_args.kwargs['wait_after_auto_ms'], 0)
            self.assertEqual(browser.call_args.kwargs['profile_name'],
                             SohuLogin._profile_name('phone'))

    def test_auto_renew_rejects_a_different_browser_account(self):
        with TemporaryDirectory() as root:
            target = Path(root) / 'sohu.json'
            target.write_text('original', encoding='utf-8')
            login = SohuLogin('phone', 'password', target, account_id='original-id')

            def browser_login(*args, **kwargs):
                login.destination.write_text('new-session', encoding='utf-8')
                return LoginResult(status='ok')

            client = Mock()
            client.account.return_value = {'id': 'other-id', 'name': 'Other'}
            with patch('mulpubcli.browser.PlaywrightLoginer') as browser, \
                 patch('mulpubcli.platforms.sohu.client.SohuWeb.load', return_value=client):
                browser.return_value.login.side_effect = browser_login
                result = login.run()

            self.assertEqual(result['status'], 'failed')
            self.assertEqual(target.read_text(encoding='utf-8'), 'original')
            client.save.assert_not_called()

    def test_cli_reuses_valid_session_without_prompting(self):
        with TemporaryDirectory() as root:
            store = StorageLayout(Path(root))
            credential = store.credentials('sohu')
            credential.parent.mkdir(parents=True, exist_ok=True)
            credential.write_text('{}', encoding='utf-8')
            expected = {'status': 'authenticated', 'platform': 'sohu', 'account_id': '123'}
            args = SimpleNamespace(refresh=False, phone=None, password=None, proxy=None)
            with patch('mulpubcli.__main__._probe_account', return_value=expected), \
                 patch('builtins.input', side_effect=AssertionError('unexpected prompt')), \
                 patch('mulpubcli.__main__._out') as output:
                code = _sohu_login(args, store)
            self.assertEqual(code, 0)
            output.assert_called_once_with(expected)

    def test_cli_new_login_reports_verified_identity(self):
        with TemporaryDirectory() as root:
            store = StorageLayout(Path(root))
            args = SimpleNamespace(refresh=True, phone='phone', password='password', proxy=None)
            verified = {'status': 'ok', 'platform': 'sohu', 'account_id': '123',
                        'username': 'Alice', 'message': '登录成功'}
            with patch('mulpubcli.platforms.sohu.login.SohuLogin.run', return_value=verified), \
                 patch('mulpubcli.__main__._out') as output:
                code = _sohu_login(args, store)
            self.assertEqual(code, 0)
            payload = output.call_args.args[0]
            self.assertEqual(payload['status'], 'authenticated')
            self.assertEqual(payload['account_id'], '123')
            self.assertEqual(payload['username'], 'Alice')
            self.assertIn('续期凭证已刷新', payload['message'])

    def test_cli_refresh_passes_saved_account_when_phone_matches(self):
        with TemporaryDirectory() as root:
            store = StorageLayout(Path(root))
            cred = store.credentials('sohu')
            cred.parent.mkdir(parents=True, exist_ok=True)
            cred.write_text(json.dumps({'account_id': 'original', 'cookies': []}),
                            encoding='utf-8')
            from mulpubcli.http import private_json
            private_json(store.login_secret('sohu'),
                         {'phone': '13500000000', 'password': 'secret'})
            args = SimpleNamespace(refresh=True, phone='13500000000',
                                   password='secret', proxy=None, show_browser=False)
            with patch('mulpubcli.platforms.sohu.login.SohuLogin') as login, \
                 patch('mulpubcli.__main__._out'):
                login.return_value.run.return_value = {'status': 'failed'}
                _sohu_login(args, store)
            self.assertEqual(login.call_args.kwargs['account_id'], 'original')
            self.assertFalse(login.call_args.kwargs['allow_human'])

    def test_cli_refresh_checks_original_account_without_reusing_other_phone_cookies(self):
        with TemporaryDirectory() as root:
            store = StorageLayout(Path(root))
            cred = store.credentials('sohu')
            cred.parent.mkdir(parents=True, exist_ok=True)
            cred.write_text(json.dumps({'account_id': 'original', 'cookies': []}),
                            encoding='utf-8')
            from mulpubcli.http import private_json
            private_json(store.login_secret('sohu'),
                         {'phone': '13500000000', 'password': 'old-secret'})
            args = SimpleNamespace(refresh=False, phone='13900000000',
                                   password='new-secret', proxy=None, show_browser=False)
            with patch('mulpubcli.platforms.sohu.login.SohuLogin') as login, \
                 patch('mulpubcli.__main__._probe_account', return_value=None), \
                 patch('mulpubcli.__main__._out'):
                login.return_value.run.return_value = {'status': 'failed'}
                _sohu_login(args, store)
            self.assertEqual(login.call_args.kwargs['account_id'], 'original')
            self.assertFalse(login.call_args.kwargs['bootstrap_existing'])

    def test_cli_can_show_browser_for_manual_page_challenge(self):
        args = _build_parser().parse_args(['login', 'sohu', '--show-browser'])
        with TemporaryDirectory() as root:
            store = StorageLayout(Path(root))
            args.refresh = True
            args.phone = 'phone'
            args.password = 'password'
            with patch.dict('os.environ', {'DISPLAY': ':1'}), \
                 patch('mulpubcli.platforms.sohu.login.SohuLogin.run',
                       return_value={'status': 'failed', 'platform': 'sohu'}) as run, \
                 patch('mulpubcli.__main__._out'):
                _sohu_login(args, store)
            run.assert_called_once_with(headless=False)


if __name__ == '__main__':
    unittest.main()
