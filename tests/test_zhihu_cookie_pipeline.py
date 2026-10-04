"""Tests for the internal Zhihu cookie pipeline used by browser login export.

覆盖浏览器登录导出的 Cookie 落盘复用到的环节：知乎域名过滤、过期剔除、
name/value 换行注入拒绝、z_c0 登录要求，以及 Records 会话构造。
"""
import unittest
from mulpubcli.platforms.zhihu import cookies as zhihu_cookies


def _record(name, value='v', domain='.zhihu.com', expires=None):
    return {'name': name, 'value': value, 'domain': domain, 'path': '/',
            'secure': False, 'expires': expires}


class ZhihuRecordsTests(unittest.TestCase):
    def test_filters_non_zhihu_domains(self):
        out = zhihu_cookies.zhihu_records([
            _record('z_c0', 'tok'), _record('g', 'x', domain='.google.com'),
        ])
        self.assertEqual([r['name'] for r in out], ['z_c0'])

    def test_drops_expired(self):
        out = zhihu_cookies.zhihu_records(
            [_record('z_c0', 'tok', expires=1500000000)], now=2000000000)
        self.assertEqual(out, [])

    def test_keeps_session_cookie(self):
        out = zhihu_cookies.zhihu_records([_record('z_c0', 'tok', expires=None)])
        self.assertEqual(len(out), 1)

    def test_rejects_newline_injection(self):
        with self.assertRaises(ValueError):
            zhihu_cookies.zhihu_records([_record('z_c0', 'ok\r\nSet-Cookie: x=1')])


class RequireLoginCookieTests(unittest.TestCase):
    def test_requires_z_c0(self):
        with self.assertRaises(ValueError):
            zhihu_cookies.require_login_cookie([_record('_xsrf', 'a')])

    def test_accepts_valid_z_c0(self):
        zhihu_cookies.require_login_cookie([_record('z_c0', 'tok')])  # 不抛异常

    def test_rejects_expired_z_c0(self):
        with self.assertRaises(ValueError):
            zhihu_cookies.require_login_cookie(
                [_record('z_c0', 'tok', expires=1500000000)], now=2000000000)


class BuildSessionTests(unittest.TestCase):
    def test_build_session_holds_cookies(self):
        session = zhihu_cookies.build_session([_record('z_c0', 'tok')])
        names = {c.name for c in session.cookies}
        self.assertIn('z_c0', names)
        self.assertEqual(dict(session.cookies)['z_c0'], 'tok')

    def test_expires_normalized(self):
        self.assertIsNone(zhihu_cookies._expires(-1))       # 会话 Cookie
        self.assertEqual(zhihu_cookies._expires(2000000000), 2000000000)
        self.assertIsNone(zhihu_cookies._expires(None))


if __name__ == '__main__':
    unittest.main()