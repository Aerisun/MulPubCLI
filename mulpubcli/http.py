"""Bounded HTTP calls and project-local, host-scoped session storage."""
from __future__ import annotations

import json
import os
import re
import tempfile
import fcntl
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

try:
    from curl_cffi.requests.exceptions import RequestException as CurlRequestException, Timeout as CurlTimeout
except ImportError:  # Optional XHS transport is installed by setup_xhs.py.
    CurlRequestException, CurlTimeout = requests.RequestException, requests.Timeout


def chrome_session(impersonate: str = 'chrome146'):
    """A curl_cffi session impersonating a real Chrome TLS fingerprint.

    与小红书已验证的传输模式一致（TLS 伪装 + HTTP/2 over TLS），用于让知乎等平台在
    登录/投稿时呈现真实浏览器指纹，避免被平台按 TLS 特征判为非浏览器。保留会话 cookie
    的自动存取（知乎登录依赖会话内 d_c0/_xsrf/z_c0），故不使用 XHS 的 discard_cookies。
    """
    from curl_cffi import requests as curl_requests
    return curl_requests.Session(impersonate=impersonate, default_headers=False,
                                 http_version='v2tls')


def _safe_target(url: str, *, base_url: str = '') -> str | None:
    """Return a host/path diagnostic without query strings or long identifiers."""
    if not isinstance(url, str) or not url or any(ord(c) < 32 for c in url):
        return None
    try:
        # Accept both full URLs and diagnostics already reduced to host/path.
        if not base_url and not url.startswith(('/', 'http://', 'https://')):
            if '.' not in url.split('/', 1)[0]:
                return None
            url = 'https://' + url
        parsed = urlsplit(urljoin(base_url, url))
        if parsed.scheme not in ('', 'http', 'https'):
            return None
        path = parsed.path or '/'
        path = re.sub(r'(/api/v3/account/api/login/qrcode/)[^/]+(?=/scan_info(?:/|$))',
                      r'\1<redacted>', path)
        path = re.sub(r'(/account/scan/login/)[^/]+(?=/|$)', r'\1<redacted>', path)
        path = re.sub(
            r'(/(?:token|access_token|password|secret|session|sessionid|authorization|cookie|api_key)/)[^/]+',
            r'\1<redacted>', path, flags=re.IGNORECASE,
        )
        path = re.sub(r'(?<=/)[A-Za-z0-9_-]{24,}(?=/|$)', '<redacted>', path)
        path = re.sub(r'(?<=/)\d{8,}(?=/|$)', '<redacted>', path)
        path = '/'.join(
            segment if segment == '<redacted>' or re.fullmatch(r'[A-Za-z0-9._-]*', segment)
            else '<redacted>' for segment in path.split('/')
        )
        path = path[:256]
        host = parsed.hostname
        if host and (len(host) > 253 or not re.fullmatch(r'[a-zA-Z0-9.-]+', host)):
            return None
    except (TypeError, ValueError):
        return None
    return f'{host}{path}' if host else path


class HTTPFailure(RuntimeError):
    """Safe diagnostics omit full URLs, query strings, cookies and response bodies."""

    def __init__(self, message: str, *, kind: str = 'http_error', status_code=None, code=None,
                 request_method=None, request_target=None, redirect_target=None):
        super().__init__(message)
        self.kind, self.status_code, self.code = kind, status_code, code
        self.request_method = request_method
        self.request_target = request_target
        self.redirect_target = redirect_target

    def as_dict(self):
        result = {'status': 'failed', 'kind': self.kind, 'message': str(self)}
        if self.status_code is not None:
            result['http_status'] = self.status_code
        if self.code is not None:
            result['platform_code'] = self.code
        if self.request_method is not None:
            result['request_method'] = self.request_method
        if self.request_target is not None:
            result['request_target'] = self.request_target
        if self.redirect_target is not None:
            result['redirect_target'] = self.redirect_target
        return result

    def safe_details(self) -> dict:
        """Whitelist diagnostics for services; never copy arbitrary exception text."""
        kinds = {
            'http_error', 'authentication_required', 'verification_required',
            'platform_rejected', 'rate_limited', 'redirected', 'network_timeout',
            'network_error', 'invalid_response', 'account_mismatch',
            'account_selection_required', 'account_setup_required', 'local_state_invalid',
            'local_environment', 'legacy_record_mismatch', 'validation', 'limit',
        }
        kind = self.kind if isinstance(self.kind, str) and self.kind in kinds else 'http_error'
        result = {'kind': kind}
        if type(self.status_code) is int and 100 <= self.status_code <= 599:
            result['http_status'] = self.status_code
        if ((type(self.code) is int and abs(self.code) < 10 ** 18)
                or (isinstance(self.code, str) and re.fullmatch(r'-?[0-9]{1,18}', self.code))):
            result['platform_code'] = self.code
        if isinstance(self.request_method, str):
            method = self.request_method.upper()
            if method in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'}:
                result['request_method'] = method
        for key in ('request_target', 'redirect_target'):
            target = _safe_target(getattr(self, key))
            if target:
                result[key] = target
        return result

    def user_message(self) -> str:
        """Describe a failure with fixed wording and safe numeric codes only."""
        details = self.safe_details()
        reason, action = {
            'authentication_required': ('登录态已失效', '请重新登录并核对平台作品列表'),
            'verification_required': ('平台要求完成安全验证', '请完成验证后核对平台作品列表'),
            'platform_rejected': ('平台拒绝了请求', '请在平台后台核对账号权限和作品状态'),
            'rate_limited': ('平台请求过于频繁', '请稍后核对平台作品状态'),
            'redirected': ('平台将请求转向其他页面', '请核对登录状态并完成平台验证'),
            'network_timeout': ('平台请求超时，未取得可靠回执', '请先核对平台作品列表'),
            'network_error': ('网络请求未取得可靠回执', '请先核对平台作品列表'),
            'invalid_response': ('平台返回了无法识别的结果', '请先核对平台作品列表'),
            'account_mismatch': ('平台账号与保存的账号不一致', '请核对账号后重新登录'),
            'account_selection_required': ('平台返回多个可用账号', '请先选择发布账号'),
            'account_setup_required': ('平台账号尚未完成开通', '请先在平台后台完成账号设置'),
            'local_state_invalid': ('保存的登录信息无效', '请重新登录后再操作'),
            'local_environment': ('本地运行环境不可用', '请检查运行环境后再操作'),
            'legacy_record_mismatch': ('保存的作品记录不一致', '请先核对原作品记录'),
            'validation': ('投稿内容未通过校验', '请检查稿件内容和图片'),
            'limit': ('平台发布额度已用完', '请等待额度恢复后再操作'),
        }.get(details['kind'], ('平台请求未成功', '请核对平台状态后再操作'))
        codes = []
        if 'http_status' in details:
            codes.append(f"HTTP {details['http_status']}")
        if 'platform_code' in details:
            codes.append(f"平台代码 {details['platform_code']}")
        return reason + (f"（{'，'.join(codes)}）" if codes else '') + '；' + action

    @classmethod
    def rejected(cls, status_code: int, payload=None, *, request_method=None, request_url=None,
                 redirect_location=None):
        error = payload.get('error') if isinstance(payload, dict) else None
        code = error.get('code') if isinstance(error, dict) else None
        code = code if type(code) is int else None
        kind = {401: 'authentication_required', 403: 'platform_rejected', 429: 'rate_limited'}.get(status_code, 'http_error')
        if 300 <= status_code < 400:
            kind = 'redirected'
        if code == 40352:
            kind = 'verification_required'
        suffix = f'，平台代码 {code}' if code is not None else ''
        request_target = _safe_target(request_url) if request_url else None
        redirect_target = (_safe_target(redirect_location, base_url=request_url)
                           if redirect_location and request_url else None)
        return cls(f'HTTP {status_code}{suffix}；已停止请求，不自动重试', kind=kind,
                   status_code=status_code, code=code, request_method=request_method,
                   request_target=request_target, redirect_target=redirect_target)


class HTTP:
    def __init__(self, hosts: set[str], *, session=None, on_response=None):
        self.hosts = hosts
        self.session = session if session is not None else requests.Session()
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
            raise HTTPFailure(f'网络请求未得到可靠结果（{type(exc).__name__}），不自动重试', kind=kind,
                              request_method=method, request_target=_safe_target(url)) from None
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
                raise HTTPFailure.rejected(
                    response.status_code,
                    payload,
                    request_method=method,
                    request_url=url,
                    redirect_location=response.headers.get('Location')
                    if 300 <= response.status_code < 400 else None,
                )
        return response

    def json(self, method: str, url: str, **kwargs) -> dict:
        response = self.request(method, url, **kwargs)
        try:
            data = response.json()
        except ValueError:
            raise HTTPFailure('平台没有返回有效 JSON，已停止', kind='invalid_response',
                              status_code=response.status_code, request_method=method,
                              request_target=_safe_target(url)) from None
        try:
            return self.checked(data, status_code=response.status_code)
        except HTTPFailure as exc:
            exc.status_code = response.status_code
            exc.request_method = method
            exc.request_target = _safe_target(url)
            raise

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
    )} for cookie in iter_cookies(session)]
    private_json(path, {'cookies': cookies, **metadata})


def iter_cookies(session_or_cookies):
    """Iterate Cookie objects from Requests and curl_cffi cookie containers."""
    cookies = getattr(session_or_cookies, 'cookies', session_or_cookies)
    jar = getattr(cookies, 'jar', None)
    return iter(jar if jar is not None else cookies)


def add_cookies(session, items) -> None:
    """Add normalized cookie records to a Requests-compatible session.

    items 是结构如 {'name','value','domain','path','secure','expires'} 的记录字典。
    这是 load_session 与浏览器 Cookie 导入（zhihu/cookies.py）共用的会话注入逻辑，
    避免两处重复实现。Playwright 用 -1 表示不设过期时间的浏览器会话 Cookie。
    """
    for item in items:
        values = {key: item[key] for key in ('name', 'value', 'domain', 'path', 'secure', 'expires') if key in item}
        if values.get('expires') == -1:
            values['expires'] = None
        cookie = requests.cookies.create_cookie(**values)
        for key in ('domain_specified', 'domain_initial_dot', 'path_specified'):
            if key in item:
                setattr(cookie, key, item[key])
        session.cookies.set_cookie(cookie)


def load_session(path: Path, hosts: set[str], *, cookie_roots: set[str] | None = None):
    if path.stat().st_mode & 0o077:
        raise ValueError('凭证文件权限过宽，请设为 600')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('cookies', []), list):
        raise ValueError('凭证文件结构无效')
    session = requests.Session()
    items = []
    for item in data.pop('cookies', []):
        if not isinstance(item, dict) or not isinstance(item.get('domain'), str):
            raise ValueError('Cookie 结构无效')
        domain = item['domain'].lstrip('.')
        allowed_for_host = any(host == domain or host.endswith('.' + domain) for host in hosts)
        allowed_related = cookie_roots is not None and any(
            domain == root or domain.endswith('.' + root) for root in cookie_roots)
        if '.' not in domain or not (allowed_for_host or allowed_related):
            continue
        if any(not isinstance(item.get(key), str) or '\r' in item[key] or '\n' in item[key] for key in ('name', 'value')):
            raise ValueError('Cookie 名称或值无效')
        items.append(item)
    add_cookies(session, items)
    return session, data
