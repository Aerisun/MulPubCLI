import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from unittest.mock import patch

import requests
from curl_cffi import requests as curl_requests

from mutipubcli.http import HTTP, HTTPFailure, save_session
from mutipubcli.platforms.toutiao.client import ToutiaoWeb
from mutipubcli.platforms.zhihu.client import ZhihuWeb


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self.payload = payload
        self.headers = headers or {}

    def json(self):
        if self.payload is None:
            raise ValueError('no JSON body')
        return self.payload


class RedirectSession:
    def __init__(self):
        self.kwargs = None

    def request(self, method, url, **kwargs):
        self.kwargs = kwargs
        return FakeResponse(
            302,
            headers={'Location': '/account/unhuman?next=private-value'},
        )


class ScriptedZhihuSession(curl_requests.Session):
    def __init__(self, token):
        super().__init__(impersonate='chrome124')
        self.token = token
        self.calls = []

    def request(self, method, url, **kwargs):
        path = urlsplit(url).path
        self.calls.append((method, path, dict(self.headers), kwargs))
        if method == 'GET' and path == '/signin':
            self.cookies.set('_xsrf', 'test-xsrf', domain='.zhihu.com', path='/')
            return FakeResponse()
        if method == 'POST' and path == '/udid':
            return FakeResponse()
        if method == 'POST' and path == '/api/v3/account/api/login/qrcode':
            return FakeResponse(payload={
                'token': self.token,
                'link': f'https://www.zhihu.com/account/scan/login/{self.token}',
                'expires_at': int(time.time()) + 120,
            })
        raise AssertionError(f'unexpected request: {method} {path}')


class HTTPAndZhihuLoginTests(unittest.TestCase):
    def test_redirect_error_identifies_sanitized_request_and_target(self):
        session = RedirectSession()
        client = HTTP({'www.zhihu.com'}, session=session)
        qr_token = 'xyZ_12-Token'
        request_url = (
            'https://www.zhihu.com/api/v3/account/api/login/qrcode/'
            f'{qr_token}/scan_info?access_token=request-secret'
        )

        with self.assertRaises(HTTPFailure) as caught:
            client.request('GET', request_url)

        result = caught.exception.as_dict()
        self.assertEqual(result['http_status'], 302)
        self.assertEqual(
            result['request_target'],
            'www.zhihu.com/api/v3/account/api/login/qrcode/<redacted>/scan_info',
        )
        self.assertEqual(result['redirect_target'], 'www.zhihu.com/account/unhuman')
        self.assertNotIn(qr_token, json.dumps(result))
        self.assertNotIn('private-value', json.dumps(result))
        self.assertNotIn('request-secret', json.dumps(result))
        self.assertIs(session.kwargs['allow_redirects'], False)

    def test_save_session_serializes_curl_cffi_cookie_objects(self):
        for session in (requests.Session(), curl_requests.Session(impersonate='chrome124')):
            session.cookies.set('probe', 'value', domain='.zhihu.com', path='/')
            try:
                with TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
                    path = Path(temp_dir) / 'session.json'
                    save_session(path, session)
                    payload = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(payload['cookies'][0]['name'], 'probe')
                self.assertEqual(payload['cookies'][0]['value'], 'value')
            finally:
                session.close()

    def test_zhihu_login_uses_signin_bootstrap_and_curl_cffi_xsrf_cookie(self):
        token = 'AbCdEf0123456789ABCDEFGHIJKLMNOP'
        session = ScriptedZhihuSession(token)
        client = ZhihuWeb(session=session)
        try:
            with TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
                qr_path = Path(temp_dir) / 'zhihu.png'
                with patch.object(client, 'poll_login', return_value={
                    'status': 'waiting', 'message': 'waiting',
                }):
                    result = client.start_login(qr_path)
                self.assertTrue(qr_path.exists())

            self.assertEqual([call[1] for call in session.calls], [
                '/signin',
                '/udid',
                '/api/v3/account/api/login/qrcode',
            ])
            self.assertEqual(session.headers['Referer'], 'https://www.zhihu.com/signin')
            self.assertEqual(session.headers['Origin'], 'https://www.zhihu.com')
            self.assertEqual(session.headers['x-xsrftoken'], 'test-xsrf')
            self.assertEqual(result['status'], 'waiting')
        finally:
            client.close()

    def test_zhihu_defaults_to_curl_cffi_chrome_session(self):
        # 知乎登录默认应使用 curl_cffi 伪装真实 Chrome 的 TLS 指纹，而不是裸 requests，
        # 否则 TLS 特征会被知乎 WAF 判为非浏览器并引导到 /account/unhuman。
        client = ZhihuWeb()
        try:
            self.assertIsInstance(client.http.session, curl_requests.Session)
            self.assertEqual(getattr(client.http.session, 'impersonate', ''), 'chrome146')
            self.assertNotIsInstance(client.http.session, requests.Session)
        finally:
            client.close()

    def test_zhihu_account_accepts_curl_cffi_cookie_jar(self):
        client = ZhihuWeb()
        client.http.session.cookies.set('z_c0', 'test-session', domain='.zhihu.com', path='/')
        try:
            with patch.object(client.http, 'json', return_value={'id': 'account-id', 'name': 'test'}):
                account = client.account()
            self.assertEqual(account['id'], 'account-id')
        finally:
            client.close()

    def test_toutiao_account_accepts_curl_cffi_cookie_jar(self):
        client = ToutiaoWeb()
        client.http.session.cookies.set('sessionid', 'test-session', domain='.toutiao.com', path='/')
        client.account_id = ''
        client.user_id = ''
        try:
            with patch.object(client, '_guard'), patch.object(client, 'call', side_effect=[
                {'is_login': True, 'user': {'id_str': '123456789', 'mobile_bind_ok': True}},
                {
                    'media': {'id_str': '987654321', 'is_enable': True},
                    'user': {'id_str': '123456789'},
                },
            ]):
                account = client.account()
            self.assertEqual(account['id'], '987654321')
        finally:
            client.close()


if __name__ == '__main__':
    unittest.main()
