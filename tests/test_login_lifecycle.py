"""Tests for the project-local login lifecycle: storage containment, credential
reuse / auto-refresh-on-expiry, session health, reset, and stuck-state recovery.
"""
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from mutipubcli.http import HTTPFailure
from mutipubcli.storage import StorageLayout
from mutipubcli.__main__ import (
    _cmd_login,
    _cmd_reset,
    _cmd_session,
    _credential_health,
)
from mutipubcli.platforms.xiaohongshu.login import (
    INITIALIZING_STALE_SECONDS,
    XHSLoginRuntime,
)
from mutipubcli.platforms.zhihu.client import ZhihuWeb

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _FakeClient:
    """Scripted platform client used to exercise the CLI login decisions."""

    def __init__(self, account_result=None):
        self.account_result = account_result
        self.start_calls = 0
        self.save_calls = 0

    def account(self):
        if isinstance(self.account_result, Exception):
            raise self.account_result
        return self.account_result or {'id': 'fake', 'name': 'fake'}

    def start_login(self, output, *, refresh=False):
        self.start_calls += 1
        return {'status': 'waiting', 'qr_image': str(output)}

    def poll_login(self):
        return {'status': 'authenticated'}

    def save(self, path=None):
        self.save_calls += 1

    def close(self):
        pass


def _args(platform='zhihu', poll=False, refresh=False, method='qr', confirm=False):
    return SimpleNamespace(platform=platform, poll=poll, refresh=refresh,
                           method=method, confirm=confirm)


class StorageContainmentTests(unittest.TestCase):
    def test_login_state_stays_below_isolated_root(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            store.credentials('zhihu').write_text(json.dumps({'account_id': 'a'}))
            store.qr_image('toutiao').write_text('png')
            root = Path(tmp).resolve()
            for p in (store.credentials('zhihu'), store.qr_image('toutiao'),
                      store.results_dir, store.tmp_dir):
                self.assertTrue(p.resolve().is_relative_to(root / '.storage'),
                                f'{p} escaped project root')

    def test_private_modes(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            cred = store.credentials('zhihu')
            cred.write_text(json.dumps({'account_id': 'a'}))
            cred.chmod(0o600)
            self.assertEqual(cred.stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.auth_dir.stat().st_mode & 0o777, 0o700)


class CredentialHealthTests(unittest.TestCase):
    def test_health_classification(self):
        cases = [
            (None, 'needs_login'),
            ({'account_id': 'a', 'cookies': [{'name': 'z_c0', 'value': 'tok'}]}, 'authenticated'),
            ({'qr_token': 't'}, 'pending_scan'),
            ({'qr_token': 't', 'qr_expires_at': 0}, 'expired'),
            ({'login_blocked': True}, 'blocked'),
        ]
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            for payload, expected in cases:
                with self.subTest(payload=payload):
                    path = store.credentials('zhihu')
                    path.unlink(missing_ok=True)
                    if payload is not None:
                        path.write_text(json.dumps(payload))
                    state = _credential_health(path, store.qr_image('zhihu'))['status']
                    self.assertEqual(state, expected)


class SessionCommandTests(unittest.TestCase):
    def test_session_reports_all_platforms(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            with patch('mutipubcli.__main__._out') as out:
                code = _cmd_session(_args(platform=None), store)
            self.assertEqual(code, 0)
            payload = out.call_args[0][0]
            self.assertIn('storage_root', payload)
            self.assertEqual(payload['sessions']['zhihu']['status'], 'needs_login')
            self.assertEqual(set(payload['sessions']), {'xiaohongshu', 'zhihu', 'toutiao'})

    def test_reset_removes_credential_and_qr(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            store.credentials('zhihu').write_text(json.dumps({'account_id': 'a'}))
            store.qr_image('zhihu').write_text('png')
            with patch('mutipubcli.__main__._out') as out:
                code = _cmd_reset(_args('zhihu'), store)
            self.assertEqual(code, 0)
            self.assertFalse(store.credentials('zhihu').exists())
            self.assertFalse(store.qr_image('zhihu').exists())
            self.assertEqual(len(out.call_args[0][0]['removed']), 2)


class LoginReuseAndRefreshTests(unittest.TestCase):
    def test_login_reuses_valid_authenticated_session(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            cred = store.credentials('zhihu')
            cred.write_text(json.dumps({'account_id': 'abc', 'cookies': [{'name': 'z_c0', 'value': 'tok'}]}))
            client = _FakeClient(account_result={'id': 'abc', 'name': 'me'})
            with patch('mutipubcli.__main__._load_client', return_value=client), \
                 patch('mutipubcli.__main__._new_client') as new_client, \
                 patch('mutipubcli.__main__._out') as out:
                code = _cmd_login(_args('zhihu'), store)
            self.assertEqual(code, 0)
            # Valid session is returned as usable; no fresh QR is minted.
            self.assertEqual(client.start_calls, 0)
            new_client.assert_not_called()
            self.assertEqual(out.call_args[0][0]['status'], 'authenticated')

    def test_login_auto_refreshes_stale_authenticated_session(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            store = StorageLayout(Path(tmp))
            cred = store.credentials('zhihu')
            cred.write_text(json.dumps({'account_id': 'abc', 'cookies': [{'name': 'z_c0', 'value': 'tok'}]}))
            stale_client = _FakeClient(account_result=HTTPFailure('登录态已失效', code=40352))
            fresh_client = _FakeClient()
            with patch('mutipubcli.__main__._load_client', side_effect=[stale_client]), \
                 patch('mutipubcli.__main__._new_client', return_value=fresh_client), \
                 patch('mutipubcli.__main__._out') as out:
                code = _cmd_login(_args('zhihu'), store)
            self.assertEqual(code, 0)
            # Probe failed => fresh anonymous QR is minted to replace the stale session.
            self.assertEqual(fresh_client.start_calls, 1)
            self.assertEqual(out.call_args[0][0]['status'], 'waiting')


class ZhihuGatedPollTests(unittest.TestCase):
    def test_poll_40352_is_non_fatal(self):
        client = ZhihuWeb()
        client.qr_token = 'token123'
        client.qr_expires_at = time.time() + 120
        client.login_blocked = False
        try:
            with patch.object(client.http, 'json',
                              side_effect=HTTPFailure('anti-bot gate', code=40352)):
                result = client.poll_login()
            self.assertEqual(result['status'], 'waiting')
            self.assertFalse(client.login_blocked)
        finally:
            client.close()


class XHSStaleRecoveryTests(unittest.TestCase):
    def _obj(self, state):
        obj = XHSLoginRuntime.__new__(XHSLoginRuntime)
        obj.state = state
        saved = []
        obj.save = lambda: saved.append(True)
        return obj, saved

    def test_stale_initializing_is_recovered(self):
        obj, saved = self._obj({'initializing': True,
                                'initializing_at': time.time() - INITIALIZING_STALE_SECONDS - 10})
        self.assertTrue(obj._recover_stale_initializing())
        self.assertNotIn('initializing', obj.state)
        self.assertEqual(len(saved), 1)

    def test_fresh_initializing_is_not_recovered(self):
        obj, saved = self._obj({'initializing': True, 'initializing_at': time.time()})
        self.assertFalse(obj._recover_stale_initializing())
        self.assertIn('initializing', obj.state)
        self.assertEqual(len(saved), 0)


if __name__ == '__main__':
    unittest.main()