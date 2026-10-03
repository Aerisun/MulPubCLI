"""Tests for browser Cookie-file import into Zhihu credentials.

覆盖四种格式（原始 Cookie 头 / Chrome 扩展 JSON / Playwright JSON /
Netscape cookies.txt）、知乎域名过滤、过期与换行注入校验、z_c0 要求、
账号核验后原子保存、失败不覆盖旧凭证，以及 CLI 的 zhihu 分支。
"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from mutipubcli.http import HTTPFailure
from mutipubcli.platforms.zhihu import cookies as zhihu_cookies
from mutipubcli.platforms.zhihu.client import ZhihuWeb
from mutipubcli.storage import StorageLayout

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 原始 Cookie 头（合成的等价样本，不含真实 token）：value 可含 | 与末尾 =，
# 用 ; 分隔、按第一个 = 切分 name/value。
RAW_HEADER = (
    "_zap=aaa-bbb; d_c0=ABC123==|1700000000; _xsrf=xyz; "
    "z_c0=2|1:0|10:1700000000|4:z_c0|92:Mi4xxx|sig_hash; "
    "__snaker__id=7LKRet; SESSIONID=ABC; osd=VVVV="
)

CHROME_JSON = json.dumps([
    {'name': 'z_c0', 'value': 'tok-z', 'domain': '.zhihu.com', 'path': '/',
     'secure': True, 'httpOnly': True, 'expirationDate': 2000000000,
     'sameSite': 'lax', 'hostOnly': False},
    {'name': 'google_c', 'value': 'x', 'domain': '.google.com', 'path': '/',
     'expirationDate': 2000000000},
])

PLAYWRIGHT_JSON = json.dumps([
    {'name': 'z_c0', 'value': 'pw-tok', 'domain': '.zhihu.com', 'path': '/',
     'expires': -1, 'httpOnly': True, 'secure': True, 'sameSite': 'Lax'},
    {'name': 'd_c0', 'value': 'pw-dev', 'domain': '.zhihu.com', 'path': '/', 'expires': -1},
])

NETSCAPE = (
    '# Netscape HTTP Cookie File\n'
    '.zhihu.com\tTRUE\t/\tFALSE\t0\tz_c0\ttok-netscape\n'
    '.google.com\tTRUE\t/\tFALSE\t0\tg\ty\n'
)


def _write_file(tmp, name, text):
    path = Path(tmp) / name
    path.write_text(text, encoding='utf-8')
    return path


def _login_args(platform='zhihu', cookie_file=None, cookie_stdin=False):
    return SimpleNamespace(platform=platform, poll=False, refresh=False,
                           method='qr', confirm=False, cookie_file=cookie_file,
                           cookie_stdin=cookie_stdin)


class RawHeaderParsingTests(unittest.TestCase):
    def test_parses_raw_cookie_header(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            records = zhihu_cookies.parse_cookie_file(_write_file(tmp, 'c.txt', RAW_HEADER))
            names = {r['name'] for r in records}
            self.assertEqual(names, {'_zap', 'd_c0', '_xsrf', 'z_c0', '__snaker__id', 'SESSIONID', 'osd'})
            by = {r['name']: r for r in records}
            # value 含 | 与尾部 = 时不被截断
            self.assertTrue(by['z_c0']['value'].startswith('2|1:0|10:1700000000'))
            self.assertTrue(by['d_c0']['value'].endswith('1700000000'))
            self.assertTrue(by['osd']['value'].endswith('='))
            # 原始头统一归属知乎 .zhihu.com
            self.assertEqual(by['z_c0']['domain'], '.zhihu.com')

    def test_accepts_cookie_prefix(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            records = zhihu_cookies.parse_cookie_file(
                _write_file(tmp, 'c.txt', 'Cookie: ' + RAW_HEADER))
            self.assertIn('z_c0', {r['name'] for r in records})

    def test_chrome_json_array(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            records = zhihu_cookies.parse_cookie_file(_write_file(tmp, 'c.txt', CHROME_JSON))
            filtered = zhihu_cookies.zhihu_records(records)
            self.assertEqual({r['name'] for r in filtered}, {'z_c0'})  # 非知乎域名被过滤
            z = filtered[0]
            self.assertTrue(z['secure'])
            self.assertTrue(z['http_only'])
            self.assertEqual(z['expires'], 2000000000)
            self.assertEqual(z['same_site'], 'lax')

    def test_playwright_json(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            records = zhihu_cookies.parse_cookie_file(_write_file(tmp, 'c.txt', PLAYWRIGHT_JSON))
            filtered = zhihu_cookies.zhihu_records(records)
            self.assertEqual({r['name'] for r in filtered}, {'z_c0', 'd_c0'})
            z = next(r for r in filtered if r['name'] == 'z_c0')
            self.assertIsNone(z['expires'])  # -1 → 会话 Cookie
            self.assertTrue(z['http_only'])
            self.assertEqual(z['same_site'], 'Lax')

    def test_netscape_cookies_txt(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            records = zhihu_cookies.parse_cookie_file(_write_file(tmp, 'c.txt', NETSCAPE))
            filtered = zhihu_cookies.zhihu_records(records)
            self.assertEqual([r['name'] for r in filtered], ['z_c0'])
            self.assertEqual(filtered[0]['value'], 'tok-netscape')


class ValidationTests(unittest.TestCase):
    def test_missing_z_c0_raises(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            records = zhihu_cookies.parse_cookie_file(
                _write_file(tmp, 'c.txt', '_xsrf=a; d_c0=b'))
            with self.assertRaises(ValueError):
                zhihu_cookies.require_login_cookie(zhihu_cookies.zhihu_records(records))

    def test_expired_z_c0_is_dropped(self):
        expired_json = json.dumps([
            {'name': 'z_c0', 'value': 'tok', 'domain': '.zhihu.com', 'path': '/',
             'expirationDate': 1500000000},
        ])
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            records = zhihu_cookies.parse_cookie_file(_write_file(tmp, 'c.txt', expired_json))
            filtered = zhihu_cookies.zhihu_records(records, now=2000000000)
            self.assertEqual(filtered, [])
            with self.assertRaises(ValueError):
                zhihu_cookies.require_login_cookie(filtered, now=2000000000)

    def test_newline_injection_rejected(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            records = zhihu_cookies.parse_cookie_file(
                _write_file(tmp, 'c.txt', 'z_c0=ok\r\nSet-Cookie: evil=x'))
            with self.assertRaises(ValueError):
                zhihu_cookies.zhihu_records(records)

    def test_non_list_json_rejected(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            with self.assertRaises(ValueError):
                zhihu_cookies.parse_cookie_file(
                    _write_file(tmp, 'c.txt', json.dumps({'cookies': []})))

    def test_malformed_json_rejected(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            with self.assertRaises(ValueError):
                zhihu_cookies.parse_cookie_file(_write_file(tmp, 'c.txt', '{oops'))

    def test_build_session_holds_cookies(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            records = zhihu_cookies.parse_cookie_file(_write_file(tmp, 'c.txt', RAW_HEADER))
            session = zhihu_cookies.build_session(zhihu_cookies.zhihu_records(records))
            names = {c.name for c in session.cookies}
            self.assertIn('z_c0', names)
            self.assertIn('d_c0', names)


class ImportFlowTests(unittest.TestCase):
    def test_import_saves_verified_credential(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            source = _write_file(tmp, 'cookies.txt', RAW_HEADER)
            dest = store.credentials('zhihu')

            def _fake_account(self):
                self.account_id = 'user-1'  # 模拟真实 account() 落盘前写回账号 id
                return {'id': 'user-1', 'name': '测试'}

            with patch.object(ZhihuWeb, 'account', _fake_account):
                info = ZhihuWeb.import_cookies(source, dest)
            self.assertEqual(info['id'], 'user-1')
            self.assertTrue(dest.is_file())
            self.assertEqual(dest.stat().st_mode & 0o777, 0o600)
            data = json.loads(dest.read_text(encoding='utf-8'))
            self.assertEqual(data['account_id'], 'user-1')
            names = {c['name'] for c in data['cookies']}
            self.assertIn('z_c0', names)
            self.assertIn('d_c0', names)
            # 不残留旧二维码登录状态
            self.assertEqual(data.get('qr_token'), '')
            self.assertIs(data.get('login_blocked'), False)

    def test_import_failure_does_not_overwrite_existing(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            source = _write_file(tmp, 'cookies.txt', RAW_HEADER)
            dest = store.credentials('zhihu')
            dest.write_text(json.dumps({'account_id': 'old', 'cookies': []}))
            dest.chmod(0o600)
            with patch.object(ZhihuWeb, 'account',
                              side_effect=HTTPFailure('登录态无效', code=403)):
                with self.assertRaises(HTTPFailure):
                    ZhihuWeb.import_cookies(source, dest)
            self.assertEqual(json.loads(dest.read_text(encoding='utf-8'))['account_id'], 'old')


def test_import_cookie_text_saves_verified_credential(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            dest = store.credentials('zhihu')

            def _fake_account(self):
                self.account_id = 'user-stdin'
                return {'id': 'user-stdin', 'name': 'n'}

            with patch.object(ZhihuWeb, 'account', _fake_account):
                info = ZhihuWeb.import_cookie_text(RAW_HEADER, dest)
            self.assertEqual(info['id'], 'user-stdin')
            self.assertTrue(dest.is_file())
            self.assertEqual(dest.stat().st_mode & 0o777, 0o600)
            data = json.loads(dest.read_text(encoding='utf-8'))
            self.assertEqual(data['account_id'], 'user-stdin')
            self.assertIn('z_c0', {c['name'] for c in data['cookies']})
            # 无回显路径不把 Cookie 明文写入破坏性位置：凭证只存内联 Cookie 记录
            self.assertNotIn(RAW_HEADER, dest.read_text(encoding='utf-8'))


class ZhihuCliLoginTests(unittest.TestCase):
    def test_zhihu_without_cookie_file_is_an_error(self):
        from mutipubcli.__main__ import _cmd_login
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            with patch('mutipubcli.__main__._out') as out:
                code = _cmd_login(_login_args('zhihu'), store)
            self.assertEqual(code, 1)
            self.assertEqual(out.call_args[0][0]['status'], 'error')

    def test_zhihu_with_cookie_file_imports_and_reports(self):
        from mutipubcli.__main__ import _cmd_login
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            source = _write_file(tmp, 'cookies.txt', RAW_HEADER)
            with patch('mutipubcli.__main__._out') as out, \
                 patch.object(ZhihuWeb, 'account', return_value={'id': 'u1', 'name': 'n'}):
                code = _cmd_login(_login_args('zhihu', cookie_file=str(source)), store)
            self.assertEqual(code, 0)
            payload = out.call_args[0][0]
            self.assertEqual(payload['status'], 'authenticated')
            self.assertEqual(payload['id'], 'u1')
            self.assertTrue(store.credentials('zhihu').is_file())


    def test_zhihu_cookie_stdin_imports_and_reports(self):
        from mutipubcli.__main__ import _cmd_login
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            with patch('mutipubcli.__main__._read_cookie_stdin', return_value=RAW_HEADER), \
                 patch('mutipubcli.__main__._out') as out, \
                 patch.object(ZhihuWeb, 'account', return_value={'id': 'u2', 'name': 'n'}):
                code = _cmd_login(_login_args('zhihu', cookie_stdin=True), store)
            self.assertEqual(code, 0)
            payload = out.call_args[0][0]
            self.assertEqual(payload['status'], 'authenticated')
            self.assertEqual(payload['id'], 'u2')
            self.assertTrue(store.credentials('zhihu').is_file())


if __name__ == '__main__':
    unittest.main()
