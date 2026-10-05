"""浏览器登录结果应使用统一、可核验的账号摘要。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from mulpubcli.__main__ import _login_result, _netease_login
from mulpubcli.storage import StorageLayout


def test_login_result_reads_cookie_expiry_when_metadata_missing():
    with TemporaryDirectory() as root:
        credential = Path(root) / 'netease.json'
        credential.write_text(json.dumps({
            'cookies': [{'name': 'NTESwebSI', 'expires': 1_800_000_000},
                        {'name': 'P_INFO', 'expires': None}],
        }), encoding='utf-8')
        result = _login_result('netease', credential, message='登录成功', info={
            'id': 'creator-id', 'name': '作者',
            'account_details': {'today_published': 2, 'daily_publish_limit': 6},
        })
        assert result['account_id'] == 'creator-id'
        assert result['username'] == '作者'
        assert result['expires_at'] == '2027-01-15T08:00:00+00:00'
        assert result['cookie_expirations']['P_INFO'] is None
        assert result['account_details']['daily_publish_limit'] == 6


def test_netease_cli_new_login_uses_authenticated_summary():
    with TemporaryDirectory() as root:
        store = StorageLayout(Path(root))
        args = SimpleNamespace(refresh=True, phone='phone', password='password', proxy=None)
        verified = {'status': 'ok', 'platform': 'netease', 'account_id': 'creator-id',
                    'username': '作者', 'account_details': {'today_published': 2},
                    'message': '登录成功'}
        with patch('mulpubcli.platforms.netease.login.NeteaseLogin.run', return_value=verified), \
             patch('mulpubcli.__main__._out') as output:
            assert _netease_login(args, store) == 0
        payload = output.call_args.args[0]
        assert payload['status'] == 'authenticated'
        assert payload['account_id'] == 'creator-id'
        assert payload['username'] == '作者'
        assert payload['account_details'] == {'today_published': 2}
        assert payload['credential_path'] == str(store.credentials('netease'))
        assert '续期凭证已刷新' in payload['message']
