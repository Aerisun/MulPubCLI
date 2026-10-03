"""Sign the verified article endpoint using a pinned first-party web encoder."""
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import requests

from mulpubcli.http import HTTPFailure

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / '.dev/references/zhihu-web/module-1514.js'
MODULE_SHA256 = 'a0aa85b2fde9cfee03cca7b7dc73a048bd31f43c239748473b89171a7db5e3e8'
BUNDLE_URL = 'https://static.zhihu.com/heifetz/7936.app.b42dc02463af4a8ee334.js'
BUNDLE_SHA256 = '1150b0238fbc3f11eb8d6647ab11e5a5931a81f1b0a41bcf9945eb372769cdc3'
PUBLISH_URL = 'https://www.zhihu.com/api/v4/content/publish'


def validate():
    try:
        if hashlib.sha256(MODULE.read_bytes()).hexdigest() != MODULE_SHA256:
            raise ValueError('source changed')
        if not shutil.which('node'):
            raise ValueError('node missing')
    except (OSError, ValueError):
        raise HTTPFailure('知乎本地签名依赖缺失或不匹配，请运行 scripts/setup_zhihu.py；尚未投稿',
                          kind='local_state_invalid') from None


def _encrypt(digest, user_agent):
    validate()
    try:
        result = subprocess.run(['node', str(ROOT / 'scripts/zhihu_sign.cjs')],
                                input=json.dumps({'digest': digest, 'user_agent': user_agent,
                                                  'module_path': str(MODULE)}),
                                text=True, capture_output=True, timeout=8)
        payload = json.loads(result.stdout)
        signature = payload.get('signature')
        if result.returncode or payload.get('version') != '3.0' or not isinstance(signature, str) or len(signature) != 64:
            raise ValueError('invalid signature')
        return signature
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        raise HTTPFailure('知乎本地签名生成失败，已停止', kind='local_state_invalid') from None


def publish_headers(session, body: str):
    # Use the actual destination's cookie scope; don't silently select another host's device.
    cookie = session.prepare_request(requests.Request('POST', PUBLISH_URL)).headers.get('Cookie', '')
    devices = re.findall(r'(?:^|;\s*)d_c0=([^;]+)', cookie)
    if len(devices) != 1:
        raise HTTPFailure('知乎设备 Cookie 缺失或存在歧义，已停止', kind='local_state_invalid')
    parts = ['101_3_3.0', '/api/v4/content/publish', devices[0]]
    if body and len(body.encode('utf-8')) <= 4096:
        parts.append(body)
    digest = hashlib.md5('+'.join(parts).encode('utf-8')).hexdigest()
    return {'x-zse-93': '101_3_3.0', 'x-zse-96': '2.0_' + _encrypt(digest, session.headers['User-Agent'])}
