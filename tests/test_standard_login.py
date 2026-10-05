"""Shared QR login behavior and credential metadata, without network calls."""
import unittest
import sys
from types import ModuleType
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import requests

from mulpubcli.login_flow import cookie_expiry_metadata, wait_for_qr_login
from mulpubcli.http import add_cookies
from mulpubcli.platforms.xiaohongshu.login import XHSPCLogin, XHSLogin, PCLoginTransport
from mulpubcli.platforms.xiaohongshu.client import XHSHTTP
from mulpubcli.platforms.toutiao.client import ToutiaoWeb
from mulpubcli.platforms.zhihu.client import ZhihuWeb
from mulpubcli.platforms.zhihu.browser_login import ZhihuBrowserLogin
from mulpubcli.browser import LoginResult, PlaywrightLoginer
from mulpubcli.storage import StorageLayout
from mulpubcli.__main__ import _build_parser, _cmd_login, _cmd_reset, _zhihu_browser_login
from mulpubcli.__main__ import _cmd_session


class QRLoginTests(unittest.TestCase):
    def test_one_command_waits_and_emits_final_state(self):
        events, saves = [], []
        replies = iter([{'status': 'waiting'}, {'status': 'authenticated', 'account_id': '42'}])
        result = wait_for_qr_login(
            platform='toutiao', qr_image='/tmp/qr.png',
            begin=lambda refresh: {'status': 'waiting', 'qr_image': '/tmp/qr.png'},
            poll=lambda: next(replies), save=lambda: saves.append(True), emit=events.append,
            interval_seconds=1, sleep=lambda seconds: None,
        )
        self.assertEqual(result['status'], 'authenticated')
        self.assertEqual([e['status'] for e in events], ['waiting'])
        self.assertEqual(len(saves), 3)

    def test_wait_ends_when_platform_reports_qr_expired(self):
        events, refreshes = [], []
        def begin(refresh):
            refreshes.append(refresh)
            return {'status': 'waiting'}
        result = wait_for_qr_login(
            platform='xiaohongshu', qr_image='/tmp/qr.png', begin=begin,
            poll=lambda: {'status': 'expired'}, save=lambda: None, emit=events.append,
            interval_seconds=1, sleep=lambda seconds: None,
        )
        self.assertEqual(result['status'], 'expired')
        self.assertEqual(refreshes, [False])
        self.assertEqual([e['status'] for e in events], ['waiting'])

    def test_account_setup_requirement_ends_wait(self):
        result = wait_for_qr_login(
            platform='toutiao', qr_image='/tmp/qr.png',
            begin=lambda refresh: {'status': 'waiting'},
            poll=lambda: {'status': 'missing_phone'}, save=lambda: None, emit=lambda event: None,
            interval_seconds=1, sleep=lambda seconds: None,
        )
        self.assertEqual(result['status'], 'missing_phone')


class CLIQRLoginTests(unittest.TestCase):
    def test_reset_zhihu_removes_only_its_browser_profile(self):
        with TemporaryDirectory() as tmp:
            store = StorageLayout(Path(tmp))
            zhihu_profile = store.auth_dir / 'zhihu-profile'
            sohu_profile = store.auth_dir / 'sohu-profile'
            zhihu_profile.mkdir(parents=True)
            sohu_profile.mkdir()
            (zhihu_profile / 'Cookies').write_bytes(b'zhihu')
            (sohu_profile / 'Cookies').write_bytes(b'sohu')
            with patch('mulpubcli.__main__._out'):
                self.assertEqual(_cmd_reset(SimpleNamespace(platform='zhihu'), store), 0)
            self.assertFalse(zhihu_profile.exists())
            self.assertTrue((sohu_profile / 'Cookies').exists())

    def test_refresh_only_exists_for_sohu_and_netease_login(self):
        parser = _build_parser()
        for platform in ('zhihu', 'xiaohongshu', 'toutiao'):
            with self.subTest(platform=platform):
                with patch('sys.stderr') as stderr, self.assertRaises(SystemExit) as raised:
                    parser.parse_args(['login', platform, '--refresh'])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn('unrecognized arguments: --refresh',
                              ''.join(call.args[0] for call in stderr.write.call_args_list))
        for platform in ('sohu', 'netease'):
            with self.subTest(platform=platform):
                self.assertTrue(parser.parse_args(['login', platform, '--refresh']).refresh)

    def test_existing_login_options_remain_usable_before_platform(self):
        args = _build_parser().parse_args(['login', '--phone', '13800000000',
                                           'sohu', '--password', 'secret'])
        self.assertEqual(args.phone, '13800000000')
        self.assertEqual(args.password, 'secret')

    def test_legacy_toutiao_poll_keeps_waiting_as_successful_check(self):
        with TemporaryDirectory() as tmp:
            store = StorageLayout(Path(tmp))
            from mulpubcli.http import private_json
            private_json(store.credentials('toutiao'), {'qr_token': 'pending'})
            client = SimpleNamespace(
                poll_login=lambda: {'status': 'waiting'},
                save=lambda path: None, close=lambda: None)
            args = SimpleNamespace(platform='toutiao', poll=True, refresh=False, proxy=None)
            with patch('mulpubcli.__main__._load_client', return_value=client), \
                 patch('mulpubcli.__main__._out'):
                self.assertEqual(_cmd_login(args, store), 0)

    def test_reset_xhs_clears_pending_login_sessions(self):
        with TemporaryDirectory() as tmp:
            store = StorageLayout(Path(tmp))
            from mulpubcli.http import private_json
            for path in (store.credentials('xiaohongshu'),
                         store.auth_dir / 'xiaohongshu-login.json',
                         store.auth_dir / 'xiaohongshu-sms-login.json'):
                private_json(path, {'login': {'ready': True}})
            with patch('mulpubcli.__main__._out'):
                _cmd_reset(SimpleNamespace(platform='xiaohongshu'), store)
            self.assertFalse((store.auth_dir / 'xiaohongshu-login.json').exists())
            self.assertFalse((store.auth_dir / 'xiaohongshu-sms-login.json').exists())

    def test_zhihu_reuses_verified_credential_without_opening_browser(self):
        with TemporaryDirectory() as tmp:
            store = StorageLayout(Path(tmp))
            from mulpubcli.http import private_json
            private_json(store.credentials('zhihu'), {'account_id': 'u1', 'username': '知乎作者'})
            args = SimpleNamespace(platform='zhihu', refresh=False, proxy=None)
            verified = {'status': 'authenticated', 'platform': 'zhihu',
                        'account_id': 'u1', 'username': '知乎作者'}
            with patch('mulpubcli.__main__._probe_account', return_value=verified), \
                 patch('mulpubcli.__main__._zhihu_browser_login') as browser, \
                 patch('mulpubcli.__main__._out') as out:
                code = _cmd_login(args, store)
            self.assertEqual(code, 0)
            browser.assert_not_called()
            self.assertEqual(out.call_args[0][0]['username'], '知乎作者')

    def test_xhs_old_pending_file_can_be_finished_with_poll(self):
        with TemporaryDirectory() as tmp:
            store = StorageLayout(Path(tmp))
            from mulpubcli.http import private_json
            private_json(store.credentials('xiaohongshu'), {'login': {'ready': True}})
            class FakeXHS:
                def __init__(self, path, *, source):
                    self.path = path
                @classmethod
                def load(cls, path, *, source):
                    return cls(path, source=source)
                def poll_login(self):
                    if not self.path.is_file():
                        raise ValueError('待扫码会话丢失')
                    return {'status': 'authenticated'}
                def save(self):
                    pass
                def export(self, path):
                    private_json(path, {'cookie': 'web_session=test', 'account_id': 'u1',
                                        'username': '旧会话用户', 'expires_at': None})
                def close(self):
                    pass
            args = SimpleNamespace(platform='xiaohongshu', poll=True, refresh=False,
                                   method='qr', confirm=False, proxy=None)
            with patch('mulpubcli.platforms.xiaohongshu.login.XHSPCLogin', FakeXHS), \
                 patch('mulpubcli.__main__._out') as out:
                code = _cmd_login(args, store)
            self.assertEqual(code, 0)
            self.assertEqual(out.call_args[0][0]['username'], '旧会话用户')

    def test_session_includes_saved_username_and_cookie_expiry(self):
        with TemporaryDirectory() as tmp:
            store = StorageLayout(Path(tmp))
            path = store.credentials('zhihu')
            from mulpubcli.http import private_json
            private_json(path, {'account_id': 'u1', 'username': '知乎作者',
                                'expires_at': '2030-01-01T00:00:00+00:00'})
            fake = SimpleNamespace(account=lambda: {'id': 'u1', 'name': '知乎作者'},
                                   close=lambda: None)
            events = []
            with patch('mulpubcli.__main__._load_client', return_value=fake), \
                 patch('mulpubcli.__main__._out', side_effect=events.append):
                _cmd_session(SimpleNamespace(platform='zhihu', proxy=None), store)
            entry = events[0]['sessions']['zhihu']
            self.assertEqual(entry['username'], '知乎作者')
            self.assertEqual(entry['expires_at'], '2030-01-01T00:00:00+00:00')

    def test_toutiao_command_waits_then_reports_identity(self):
        with TemporaryDirectory() as tmp:
            store = StorageLayout(Path(tmp))
            class FakeClient:
                username = '头条作者'
                account_id = '123'
                expires_at = None
                def start_login(self, output, *, refresh=False):
                    return {'status': 'waiting', 'qr_image': str(output)}
                def poll_login(self):
                    return {'status': 'authenticated'}
                def save(self, path):
                    from mulpubcli.http import private_json
                    private_json(path, {'account_id': self.account_id,
                                        'username': self.username, 'expires_at': None})
                def close(self):
                    pass
            events = []
            args = SimpleNamespace(platform='toutiao', poll=False, refresh=False, proxy=None)
            with patch('mulpubcli.__main__._new_client', return_value=FakeClient()), \
                 patch('mulpubcli.__main__._out', side_effect=events.append), \
                 patch('mulpubcli.login_flow.time.sleep'):
                code = _cmd_login(args, store)
            self.assertEqual(code, 0)
            self.assertEqual([event['status'] for event in events],
                             ['waiting', 'authenticated'])
            self.assertEqual(events[-1]['username'], '头条作者')

    def test_xhs_command_exports_publish_credential_after_scan(self):
        with TemporaryDirectory() as tmp:
            store = StorageLayout(Path(tmp))
            class FakeXHS:
                def __init__(self, path, *, source):
                    self.path = path
                def start_login(self, output, *, refresh=False):
                    return {'status': 'waiting', 'qr_image': str(output)}
                def poll_login(self):
                    return {'status': 'authenticated'}
                def save(self):
                    from mulpubcli.http import private_json
                    private_json(self.path, {'login': {'ready': True}})
                def export(self, path):
                    from mulpubcli.http import private_json
                    private_json(path, {'cookie': 'web_session=test', 'account_id': 'u1',
                                        'username': '小红书作者', 'expires_at': None})
                def close(self):
                    pass
            events = []
            args = SimpleNamespace(platform='xiaohongshu', poll=False, refresh=False,
                                   method='qr', confirm=False, proxy=None)
            with patch('mulpubcli.platforms.xiaohongshu.login.XHSPCLogin', FakeXHS), \
                 patch('mulpubcli.__main__._out', side_effect=events.append), \
                 patch('mulpubcli.login_flow.time.sleep'):
                code = _cmd_login(args, store)
            self.assertEqual(code, 0)
            self.assertEqual([event['status'] for event in events],
                             ['waiting', 'authenticated'])
            self.assertEqual(events[-1]['username'], '小红书作者')
            self.assertTrue(store.credentials('xiaohongshu').is_file())


class CookieMetadataTests(unittest.TestCase):
    def test_real_expiry_and_session_cookie_are_distinguished(self):
        metadata = cookie_expiry_metadata([
            {'name': 'z_c0', 'expires': 2_000_000_000},
            {'name': 'd_c0', 'expires': None},
        ], auth_names=('z_c0',))
        self.assertEqual(metadata['expires_at'], datetime.fromtimestamp(
            2_000_000_000, timezone.utc).isoformat())
        self.assertEqual(metadata['cookie_expirations']['d_c0'], None)

    def test_unknown_expiry_is_not_invented(self):
        metadata = cookie_expiry_metadata([
            {'name': 'web_session', 'expires': -1},
        ], auth_names=('web_session',))
        self.assertIsNone(metadata['expires_at'])
        self.assertIsNone(metadata['cookie_expirations']['web_session'])


class PlatformIdentityTests(unittest.TestCase):
    def test_zhihu_browser_uses_dedicated_profile(self):
        with TemporaryDirectory() as tmp:
            login = ZhihuBrowserLogin(Path(tmp) / 'zhihu.json')
            with patch('mulpubcli.browser.PlaywrightLoginer') as factory:
                factory.return_value.login.return_value = LoginResult(status='ok')
                login.run()
            self.assertEqual(factory.call_args.kwargs['profile_name'], 'zhihu-profile')
            self.assertEqual(factory.return_value.login.call_args.kwargs[
                'wait_for_human_update'].__self__, login)

    def test_xhs_legacy_guessed_expiry_is_cleared_on_save(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'xiaohongshu.json'
            from mulpubcli.http import private_json
            private_json(path, {'cookie': 'web_session=test',
                                'expires_at': '2030-01-01 00:00:00'})
            client = XHSHTTP.__new__(XHSHTTP)
            client.credentials = path
            client.auth = SimpleNamespace(
                cookies='web_session=test', profile=SimpleNamespace(),
                _cookie_store=SimpleNamespace(export_state=lambda: {}))
            with patch('mulpubcli.platforms.xiaohongshu.client.dump_profile', return_value={}):
                client.save()
            import json
            data = json.loads(path.read_text())
            self.assertIsNone(data['expires_at'])
            self.assertEqual(data['cookie_expirations'], {})

    def test_xhs_rotated_auth_cookie_updates_saved_expiry(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'xiaohongshu.json'
            from mulpubcli.http import private_json
            private_json(path, {'cookie': 'web_session=old',
                                'expires_at': '2029-01-01T00:00:00+00:00',
                                'cookie_expirations': {'web_session': '2029-01-01T00:00:00+00:00'}})
            jar = requests.cookies.RequestsCookieJar()
            jar.set('web_session', 'new', domain='.xiaohongshu.com', expires=2_000_000_000)
            response = SimpleNamespace(url='https://creator.xiaohongshu.com/', cookies=jar)
            client = XHSHTTP.__new__(XHSHTTP)
            client.credentials = path
            client.auth = SimpleNamespace(
                cookies='web_session=new', profile=SimpleNamespace(cookie_map={}),
                _cookie_store=SimpleNamespace(merge_response=lambda *args: None,
                                              export_state=lambda: {}),
                update_cookies=lambda *args, **kwargs: None)
            with patch('mulpubcli.platforms.xiaohongshu.client.dump_profile', return_value={}):
                client._remember_response(response)
            import json
            data = json.loads(path.read_text())
            expected = datetime.fromtimestamp(2_000_000_000, timezone.utc).isoformat()
            self.assertEqual(data['expires_at'], expected)
            self.assertEqual(data['cookie_expirations']['web_session'], expected)

    def test_xhs_next_login_replaces_expired_qr(self):
        login = XHSPCLogin.__new__(XHSPCLogin)
        login.state = {'ready': True, 'qr_expired': True, 'qr_id': 'old'}
        login.api = SimpleNamespace(
            profile=SimpleNamespace(cookie_map={}),
            generate_qrcode=lambda cookies: (True, 'ok',
                {'qr_id': 'new', 'code': 'code', 'qr_url': 'https://example.com/qr'}),
        )
        login.prepare = lambda: None
        login.save = lambda: None
        with patch('mulpubcli.platforms.xiaohongshu.login.write_qr'):
            result = login.start_login(Path('/tmp/xhs-qr.png'))
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(login.state['qr_id'], 'new')

    def test_xhs_sms_export_does_not_invent_one_year_expiry(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'xiaohongshu.json'
            login = XHSLogin.__new__(XHSLogin)
            login.state = {'authenticated': True, 'account_id': 'u1',
                           'username': '短信用户'}
            login.api = SimpleNamespace(
                profile=SimpleNamespace(cookies='web_session=test'),
                host_cookie_state=lambda: {},
                http=SimpleNamespace(cookie_expirations=[]),
            )
            with patch('mulpubcli.platforms.xiaohongshu.login.dump_profile', return_value={}):
                login.export(path)
            import json
            data = json.loads(path.read_text())
            self.assertEqual(data['username'], '短信用户')
            self.assertIsNone(data['expires_at'])

    def test_toutiao_waits_until_platform_reports_expired(self):
        import time
        client = ToutiaoWeb()
        client.qr_token = 'token'
        client.qr_created_at = time.time() - 121
        with patch.object(client, '_json', return_value={'status': '1'}) as request:
            result = client.poll_login()
        request.assert_called_once()
        self.assertEqual(result['status'], 'waiting')
        with patch.object(client, '_json', return_value={'status': 'expired'}):
            result = client.poll_login()
        self.assertEqual(result['status'], 'expired')
        self.assertTrue(client.qr_expired)
        client.close()

    def test_toutiao_next_login_replaces_platform_expired_qr(self):
        import base64
        from io import BytesIO
        from PIL import Image
        png = BytesIO()
        Image.new('RGB', (1, 1)).save(png, format='PNG')
        payload = {'token': 'new-token',
                   'qrcode': 'data:image/png;base64,' + base64.b64encode(png.getvalue()).decode()}
        client = ToutiaoWeb()
        client.qr_token = 'expired-token'
        client.qr_expired = True
        with TemporaryDirectory() as tmp:
            image_path = Path(tmp) / 'toutiao.png'
            image_path.write_bytes(b'old')
            with patch.object(client, '_json', return_value=payload):
                result = client.start_login(image_path)
            self.assertEqual(result['status'], 'waiting')
            self.assertEqual(client.qr_token, 'new-token')
            self.assertFalse(client.qr_expired)
        client.close()

    def test_generic_browser_wait_ends_on_expired_signal(self):
        loginer = PlaywrightLoginer(storage_state_dir=Path('/tmp'))
        page = SimpleNamespace(wait_for_timeout=lambda ms: None)
        result = loginer._wait_for_human(lambda page: 'expired', page, 1, None,
                                         LoginResult(status='need_human'))
        self.assertEqual(result.status, 'expired')

    def test_browser_wait_uses_passive_update_callback(self):
        loginer = PlaywrightLoginer(storage_state_dir=Path('/tmp'))
        updates = []
        page = SimpleNamespace(wait_for_timeout=lambda ms: self.fail('不应执行定时轮询'))
        result = loginer._wait_for_human(
            lambda page: True, page, 1000, None,
            LoginResult(status='need_human'),
            wait_for_update=lambda page, remaining_ms: updates.append(remaining_ms))
        self.assertEqual(result.status, 'ok')
        self.assertEqual(updates, [None])

    def test_browser_wait_never_exceeds_total_deadline_after_page_updates(self):
        loginer = PlaywrightLoginer(storage_state_dir=Path('/tmp'))
        clock = [0.0]
        waits = []
        checks = []
        page = SimpleNamespace(wait_for_timeout=lambda ms: self.fail('不应执行定时轮询'))

        def wait_for_update(page, remaining_ms):
            waits.append(remaining_ms)
            clock[0] += min(5_000, remaining_ms) / 1000

        with patch('mulpubcli.browser.time.monotonic', side_effect=lambda: clock[0]):
            result = loginer._wait_for_human(
                lambda page: checks.append(True) or False, page, 1000, 120_000,
                LoginResult(status='need_human'), wait_for_update=wait_for_update)
        self.assertEqual(result.status, 'failed')
        self.assertTrue(result.timed_out)
        self.assertEqual(clock[0], 120.0)
        self.assertEqual(len(waits), 24)
        self.assertEqual(waits[0], 120_000)
        self.assertEqual(waits[-1], 5_000)
        self.assertEqual(len(checks), 23)

    def test_zhihu_keeps_waiting_while_page_refreshes_expired_qr(self):
        login = ZhihuBrowserLogin(Path('/tmp/zhihu-test.json'))
        page = SimpleNamespace(context=SimpleNamespace(cookies=lambda: []),
                               evaluate=lambda script: '二维码已过期，请刷新')
        self.assertFalse(login._human_done(page))

    def test_xhs_transport_remembers_server_cookie_expiry(self):
        jar = requests.cookies.RequestsCookieJar()
        jar.set('web_session', 'session', domain='.xiaohongshu.com', expires=2_000_000_000)
        response = SimpleNamespace(status_code=200, cookies=jar,
                                   json=lambda: {'success': True})
        wire = SimpleNamespace(request=lambda *args, **kwargs: response)
        transport = PCLoginTransport(wire)
        transport.request('GET', 'https://www.xiaohongshu.com/')
        self.assertEqual(transport.cookie_expirations,
                         [{'name': 'web_session', 'expires': 2_000_000_000}])

    def test_xhs_transport_reads_max_age_when_cookie_jar_is_empty(self):
        from curl_cffi.requests.headers import Headers
        response = SimpleNamespace(
            status_code=200, cookies=requests.cookies.RequestsCookieJar(),
            headers=Headers([('Set-Cookie', 'web_session=secret; Max-Age=60; Path=/')]),
            json=lambda: {'success': True})
        transport = PCLoginTransport(SimpleNamespace(request=lambda *a, **k: response))
        with patch('mulpubcli.login_flow.time.time', return_value=1000):
            transport.request('GET', 'https://www.xiaohongshu.com/')
        self.assertEqual(transport.cookie_expirations,
                         [{'name': 'web_session', 'expires': 1060}])

    def test_xhs_final_qr_response_discards_guest_session_expiry(self):
        response = SimpleNamespace(status_code=200,
                                   cookies=requests.cookies.RequestsCookieJar(),
                                   json=lambda: {'success': True})
        transport = PCLoginTransport(SimpleNamespace(request=lambda *a, **k: response))
        transport.cookie_expirations = [{'name': 'web_session', 'expires': 2_000_000_000}]
        transport.request('GET',
            'https://www.xiaohongshu.com/api/sns/web/v1/login/qrcode/status')
        self.assertEqual(transport.cookie_expirations,
                         [{'name': 'web_session', 'expires': None}])

    def test_toutiao_account_exposes_and_saves_username(self):
        client = ToutiaoWeb()
        client.http.session.cookies.set('sessionid', 'test', domain='.toutiao.com')
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'toutiao.json'
            with patch.object(client, 'call', side_effect=[
                {'is_login': True, 'user': {'mobile_bind_ok': True}},
                {'media': {'id_str': '123', 'is_enable': True, 'name': '头条作者'},
                 'user': {'id_str': '456'}},
            ]):
                info = client.account()
            client.save(path)
            loaded = ToutiaoWeb.load(path)
            try:
                self.assertEqual(info['name'], '头条作者')
                self.assertEqual(loaded.username, '头条作者')
            finally:
                loaded.close()
        client.close()

    def test_zhihu_username_and_cookie_expiry_survive_save(self):
        client = ZhihuWeb(session=requests.Session())
        add_cookies(client.http.session, [{'name': 'z_c0', 'value': 'test',
                                           'domain': '.zhihu.com', 'path': '/',
                                           'expires': 2_000_000_000}])
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'zhihu.json'
            with patch.object(client.http, 'json', return_value={'id': 'abc', 'name': '知乎作者'}):
                client.account()
            client.save(path)
            loaded = ZhihuWeb.load(path)
            try:
                self.assertEqual(loaded.username, '知乎作者')
                self.assertIsNotNone(loaded.expires_at)
            finally:
                loaded.close()
        client.close()

    def test_xhs_export_writes_verified_identity_without_guessed_expiry(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'xiaohongshu.json'
            from mulpubcli.http import private_json
            private_json(path, {'login': {'ready': True},
                                'profile': {'cookies': {'a1': 'device'}, 'pc_only': True}})
            login = XHSPCLogin.__new__(XHSPCLogin)
            login.source = Path(tmp)
            login.state = {'authenticated': True, 'account_id': 'u1', 'username': '小红书作者'}
            login.api = SimpleNamespace(
                profile=SimpleNamespace(cookie_map={'a1': 'device', 'web_session': 'session'}),
                host_cookie_state=lambda: {},
                http=SimpleNamespace(cookie_expirations=[{'name': 'web_session', 'expires': None}]),
            )
            class FakeClient:
                def __init__(self, credentials, *, source):
                    import json
                    if 'profile' in json.loads(credentials.read_text()):
                        raise AssertionError('PC 登录档不应当作创作者签名档复用')
                    self.auth = SimpleNamespace(close=lambda: None)
                def call(self, *args, **kwargs):
                    return {'data': {'userId': 'u1'}}
                def save(self):
                    pass
            state_module = ModuleType('xhs_utils.xhs_pc.state')
            state_module.cookie_header = lambda cookies: '; '.join(
                f'{name}={value}' for name, value in cookies.items())
            with patch('mulpubcli.platforms.xiaohongshu.login.dump_profile', return_value={'cookies': {'a1': 'device'}}), \
                 patch('mulpubcli.platforms.xiaohongshu.client.XHSHTTP', FakeClient), \
                 patch.dict(sys.modules, {'xhs_utils': ModuleType('xhs_utils'),
                                          'xhs_utils.xhs_pc': ModuleType('xhs_utils.xhs_pc'),
                                          'xhs_utils.xhs_pc.state': state_module}):
                login.export(path)
            import json
            data = json.loads(path.read_text())
            self.assertEqual(data['account_id'], 'u1')
            self.assertEqual(data['username'], '小红书作者')
            self.assertIsNone(data['expires_at'])
            self.assertIsNone(data['cookie_expirations']['web_session'])


if __name__ == '__main__':
    unittest.main()
