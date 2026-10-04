"""搜狐登录须核验账号，并复用已验证的凭证。"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mulpubcli.__main__ import _sohu_login
from mulpubcli.browser import LoginResult
from mulpubcli.http import HTTPFailure
from mulpubcli.platforms.sohu.client import SohuWeb
from mulpubcli.platforms.sohu.login import SohuLogin
from mulpubcli.storage import StorageLayout


class SohuLoginTests(unittest.TestCase):
    def test_fast_path_does_not_add_long_fixed_sleeps(self):
        login = SohuLogin('phone', 'password', Path('/tmp/unused-sohu.json'))
        page = Mock()
        page.url = 'https://mp.sohu.com/mpfe/v4/contentManagement/news/addarticle'
        page.evaluate.return_value = ''
        with patch.object(login, '_export'):
            login._perform(page, None, None)
        waits = [call.args[0] for call in page.wait_for_timeout.call_args_list]
        self.assertFalse(any(wait >= 4_000 for wait in waits))
        self.assertGreaterEqual(page.wait_for_function.call_count, 2)

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


if __name__ == '__main__':
    unittest.main()
