"""Bounded HTTP calls and project-local, host-scoped session storage."""
from __future__ import annotations

import json
import os
import tempfile
import fcntl
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import requests

try:
    from curl_cffi.requests.exceptions import RequestException as CurlRequestException, Timeout as CurlTimeout
except ImportError:  # Optional XHS transport is installed by setup_xhs.py.
    CurlRequestException, CurlTimeout = requests.RequestException, requests.Timeout


class HTTPFailure(RuntimeError):
    """Safe error text: never include request URLs, cookies or response bodies."""

    def __init__(self, message: str, *, kind: str = 'http_error', status_code=None, code=None):
        super().__init__(message)
        self.kind, self.status_code, self.code = kind, status_code, code

    def as_dict(self):
        result = {'status': 'failed', 'kind': self.kind, 'message': str(self)}
        if self.status_code is not None:
            result['http_status'] = self.status_code
        if self.code is not None:
            result['platform_code'] = self.code
        return result

    @classmethod
    def rejected(cls, status_code: int, payload=None):
        error = payload.get('error') if isinstance(payload, dict) else None
        code = error.get('code') if isinstance(error, dict) else None
        code = code if type(code) is int else None
        kind = {401: 'authentication_required', 403: 'platform_rejected', 429: 'rate_limited'}.get(status_code, 'http_error')
        if 300 <= status_code < 400:
            kind = 'redirected'
        if code == 40352:
            kind = 'verification_required'
        suffix = f'，平台代码 {code}' if code is not None else ''
        return cls(f'HTTP {status_code}{suffix}；已停止请求，不自动重试', kind=kind, status_code=status_code, code=code)


class HTTP:
    def __init__(self, hosts: set[str], *, session=None, on_response=None):
        self.hosts = hosts
        if session is not None:
            self.session = session
        else:
            try:
                from curl_cffi import requests as crequests
                self.session = crequests.Session(impersonate="chrome124")
            except ImportError:
                self.session = requests.Session()
        self.on_response = on_response
        self.on_response = on_response

    def request(self, method: str, url: str, *, accepted_statuses=None, **kwargs):
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.hostname not in self.hosts
                or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443)):
            raise ValueError('HTTP 目标不在本平台允许的域名中')
        try:
            response = self.session.request(method, url, timeout=(10, 30), allow_redirects=False, **kwargs)
        except (requests.RequestException, CurlRequestException) as exc:
            kind = 'network_timeout' if isinstance(exc, (requests.Timeout, CurlTimeout)) else 'network_error'
            raise HTTPFailure(f'网络请求未得到可靠结果（{type(exc).__name__}），不自动重试', kind=kind) from None
        if self.on_response:
            self.on_response(response)
        if not 200 <= response.status_code < 300:
            if accepted_statuses is not None and response.status_code in accepted_statuses:
                pass
            else:
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                raise HTTPFailure.rejected(response.status_code, payload)
        return response

    def json(self, method: str, url: str, **kwargs) -> dict:
        response = self.request(method, url, **kwargs)
        try:
            data = response.json()
        except ValueError:
            raise HTTPFailure('平台没有返回有效 JSON，已停止') from None
        return self.checked(data, status_code=response.status_code)

    @staticmethod
    def checked(data, *, status_code=200):
        if not isinstance(data, dict):
            raise HTTPFailure('平台响应结构变化，已停止', kind='invalid_response')
        if data.get('error'):
            raise HTTPFailure.rejected(status_code, data)
        return data


def private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.private-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as target:
            json.dump(payload, target, ensure_ascii=False, indent=2)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def session_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path.with_suffix('.lock'), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HTTPFailure('该平台已有操作进行中，请等待完成后再试') from None
        yield
    finally:
        os.close(fd)


def save_session(path: Path, session, **metadata) -> None:
    cookies = [{key: getattr(cookie, key) for key in (
        'name', 'value', 'domain', 'path', 'secure', 'expires',
        'domain_specified', 'domain_initial_dot', 'path_specified',
    )} for cookie in session.cookies]
    private_json(path, {'cookies': cookies, **metadata})


def load_session(path: Path, hosts: set[str]):
    if path.stat().st_mode & 0o077:
        raise ValueError('凭证文件权限过宽，请设为 600')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('cookies', []), list):
        raise ValueError('凭证文件结构无效')
    session = requests.Session()
    for item in data.pop('cookies', []):
        if not isinstance(item, dict) or not isinstance(item.get('domain'), str):
            raise ValueError('Cookie 结构无效')
        domain = item['domain'].lstrip('.')
        if '.' not in domain or not any(host == domain or host.endswith('.' + domain) for host in hosts):
            continue
        if any(not isinstance(item.get(key), str) or '\r' in item[key] or '\n' in item[key] for key in ('name', 'value')):
            raise ValueError('Cookie 名称或值无效')
        values = {key: item[key] for key in ('name', 'value', 'domain', 'path', 'secure', 'expires') if key in item}
        if values.get('expires') == -1:
            values['expires'] = None  # Playwright uses -1 for a non-expiring browser-session cookie.
        cookie = requests.cookies.create_cookie(**values)
        for key in ('domain_specified', 'domain_initial_dot', 'path_specified'):
            if key in item:
                setattr(cookie, key, item[key])
        session.cookies.set_cookie(cookie)
    return session, data
