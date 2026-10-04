"""网易浏览器登录只在 HTTP 核验通过后替换已有凭证。"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from mulpubcli.browser import LoginResult
from mulpubcli.http import HTTPFailure
from mulpubcli.http import iter_cookies
from mulpubcli.platforms.netease.client import NeteaseWeb
from mulpubcli.platforms.netease.login import NeteaseLogin


class NeteaseLoginTests(unittest.TestCase):
    def test_account_reports_publish_quota_from_verified_response(self):
        client = NeteaseWeb.__new__(NeteaseWeb)
        client._authenticated = lambda: True
        client.http = Mock()
        client.http.json.return_value = {'code': 1, 'data': {
            'wemediaId': 'creator_id', 'tname': '作者', 'articleCount': 8,
            'todayPubCount': 2, 'maxDailyPublishCount': 6,
        }}
        client.wemedia_id = ''
        client.tname = ''
        client.path = None
        info = client.account()
        self.assertEqual(info['account_details'], {
            'article_count': 8, 'today_published': 2, 'daily_publish_limit': 6,
        })

    def test_export_keeps_parent_domain_cookies_needed_for_account(self):
        with TemporaryDirectory() as root:
            target = Path(root) / 'netease.json'
            login = NeteaseLogin('phone', 'password', target)
            cookies = [
                {'name': 'NTESwebSI', 'value': 'session', 'domain': 'mp.163.com',
                 'path': '/', 'secure': False, 'expires': -1},
                {'name': 'P_INFO', 'value': 'account', 'domain': '.163.com',
                 'path': '/', 'secure': False, 'expires': -1},
                {'name': 'S_INFO', 'value': 'sign', 'domain': '.163.com',
                 'path': '/', 'secure': False, 'expires': -1},
            ]
            context = Mock()
            context.cookies.return_value = cookies

            login._export(context)
            client = NeteaseWeb.load(target)
            try:
                names = {cookie.name for cookie in iter_cookies(client.http.session)}
            finally:
                client.close()

            self.assertEqual(names, {'NTESwebSI', 'P_INFO', 'S_INFO'})

    def test_failed_verification_preserves_existing_credential(self):
        with TemporaryDirectory() as root:
            target = Path(root) / 'netease.json'
            target.write_text('existing', encoding='utf-8')
            login = NeteaseLogin('phone', 'password', target)

            def browser_login(*args, **kwargs):
                login.destination.write_text('unverified', encoding='utf-8')
                return LoginResult(status='ok')

            with patch('mulpubcli.browser.PlaywrightLoginer') as browser, \
                 patch('mulpubcli.platforms.netease.client.NeteaseWeb.load',
                       side_effect=HTTPFailure('not logged in')):
                browser.return_value.login.side_effect = browser_login
                result = login.run()

            self.assertEqual(result['status'], 'failed')
            self.assertEqual(target.read_text(encoding='utf-8'), 'existing')
            self.assertEqual(login.destination, target)
            self.assertEqual(list(Path(root).glob('.netease-login-*')), [])

    def test_verified_credential_replaces_existing_credential(self):
        with TemporaryDirectory() as root:
            target = Path(root) / 'netease.json'
            target.write_text('existing', encoding='utf-8')
            login = NeteaseLogin('phone', 'password', target)

            def browser_login(*args, **kwargs):
                login.destination.write_text('exported', encoding='utf-8')
                return LoginResult(status='ok')

            client = Mock()
            client.account.return_value = {'id': 'creator-id'}
            client.save.side_effect = lambda path: path.write_text('verified', encoding='utf-8')
            with patch('mulpubcli.browser.PlaywrightLoginer') as browser, \
                 patch('mulpubcli.platforms.netease.client.NeteaseWeb.load', return_value=client):
                browser.return_value.login.side_effect = browser_login
                result = login.run()

            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['account_id'], 'creator-id')
            self.assertEqual(target.read_text(encoding='utf-8'), 'verified')
            client.close.assert_called_once()
            self.assertEqual(login.destination, target)
            self.assertEqual(browser.return_value.login.call_args.kwargs['wait_after_auto_ms'], 0)


if __name__ == '__main__':
    unittest.main()
