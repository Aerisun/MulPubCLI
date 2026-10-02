"""Single-session QR/SMS login: no browser, CAPTCHA solver, retries or device rotation."""
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
import json
import os
import re
import tempfile
import subprocess

from mutipubcli.http import HTTPFailure, private_json
from .session import load_source, dump_profile, restore_profile, restore_pc_profile


class LoginTransport:
    HOSTS = {'creator.xiaohongshu.com', 'customer.xiaohongshu.com', 'as.xiaohongshu.com', 'edith.xiaohongshu.com'}

    def __init__(self, wire):
        self.wire, self.blocked = wire, False
        self.last_status, self.last_path = None, ''

    def _status_allowed(self, method, url, response):
        return 200 <= response.status_code < 300

    def request(self, method, url, **kwargs):
        if self.blocked:
            raise HTTPFailure('本次登录会话已停止，不会继续请求或自动重建')
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname not in self.HOSTS or parsed.username or parsed.port not in (None, 443):
            raise ValueError('小红书登录目标不在允许域名中')
        kwargs.update(timeout=(10, 30), allow_redirects=False)
        self.last_path = parsed.path
        try:
            response = self.wire.request(method, url, **kwargs)
        except Exception:
            self.blocked = True
            raise HTTPFailure('小红书登录请求结果不确定，已停止且不自动重试') from None
        self.last_status = response.status_code
        if not self._status_allowed(method, url, response):
            self.blocked = True
            raise HTTPFailure(f'小红书登录 HTTP {response.status_code}，已停止且不自动重试')
        # The reference can otherwise ignore a JSON error and proceed to the next stage.
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and (body.get('code') in (300012, 300013, -9601, -9602) or body.get('captcha')):
            self.blocked = True
            raise HTTPFailure('小红书要求额外验证，已停止')
        return response

    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)

    def post(self, url, **kwargs):
        return self.request('POST', url, **kwargs)

    def close(self):
        self.wire.close()


class PCLoginTransport(LoginTransport):
    HOSTS = {'www.xiaohongshu.com', 'pages.xiaohongshu.com', 'as.xiaohongshu.com', 'edith.xiaohongshu.com'}

    def _status_allowed(self, method, url, response):
        if super()._status_allowed(method, url, response):
            return True
        return (method == 'GET' and url == 'https://www.xiaohongshu.com/' and response.status_code == 302
                and response.headers.get('Location') in ('/explore', 'https://www.xiaohongshu.com/explore'))


def write_qr(link: str, output: Path):
    if not isinstance(link, str) or not link.startswith(('http://', 'https://', 'xhsdiscover://')):
        raise HTTPFailure('小红书二维码内容格式不符')
    import qrcode
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'wb') as target:
        qrcode.make(link).save(target, format='PNG')


class XHSLoginRuntime:
    def __init__(self, path: Path, *, source: Path):
        self.source = load_source(source)
        # Fail locally before any login request if the source's Node dependency is missing.
        try:
            check = subprocess.run(['node', '-e',
                "require.resolve('crypto-js',{paths:[process.argv[1]]})", str(self.source)],
                capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('小红书登录需要可用的本地 Node.js') from None
        if check.returncode:
            raise ValueError('小红书登录缺少项目内 crypto-js，请按 README 执行 npm ci')
        from loguru import logger
        logger.disable('apis.xhs_creator_login_apis')
        logger.disable('apis.xhs_pc_login_apis')
        logger.disable('xhs_utils')
        self.path, self.state = path, {}

    @contextmanager
    def local_runtime(self):
        directory = self.source.parent.parent / 'tmp'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        previous = tempfile.tempdir
        tempfile.tempdir = str(directory)
        try:
            yield
        finally:
            tempfile.tempdir = previous
            self.save()

    def close(self):
        self.api.close()


class XHSLogin(XHSLoginRuntime):
    def __init__(self, path: Path, *, source: Path):
        super().__init__(path, source=source)
        from apis.xhs_creator_login_apis import XHSCreatorLoginApi
        from xhs_utils.xhs_creator.http import CreatorHttpClient

        class SingleAttemptAPI(XHSCreatorLoginApi):
            def _send_with_gate_retry(self, send_once, **kwargs):
                return send_once()

            def _debug_dump(self, *args, **kwargs):
                pass

            def _reset_anonymous_session(self):
                raise HTTPFailure('不会自动重建小红书登录设备会话')

        self.api = SingleAttemptAPI(http_client=LoginTransport(CreatorHttpClient()))

    @classmethod
    def load(cls, path: Path, *, source: Path):
        if path.stat().st_mode & 0o077:
            raise ValueError('登录会话文件须为 600 权限')
        data = json.loads(path.read_text())
        client = cls(path, source=source)
        from xhs_utils.xhs_core.cookies import HostCookieStore
        client.api.profile = restore_profile(data['profile'])
        client.api._cookie_store = HostCookieStore.from_state(data['host_cookie_state'])
        client.state = data['login']
        for name, value in data['security'].items():
            if name in ('_security_started', '_security_bootstrapped', '_security_completed', '_pending_dsl', '_pending_ds_program'):
                setattr(client.api, name, value)
        client.api.http.blocked = data.get('blocked', False)
        client.api.http.last_path = data.get('last_response', {}).get('path', '')
        client.api.http.last_status = data.get('last_response', {}).get('http_status')
        return client

    def save(self, path=None):
        private_json(path or self.path, {
            'profile': dump_profile(self.api.profile), 'host_cookie_state': self.api.host_cookie_state(),
            'login': self.state, 'blocked': self.api.http.blocked,
            'last_response': {'path': self.api.http.last_path, 'http_status': self.api.http.last_status},
            'security': {name: getattr(self.api, name) for name in (
                '_security_started', '_security_bootstrapped', '_security_completed', '_pending_dsl', '_pending_ds_program')},
        })

    def prepare(self):
        if self.api.http.blocked:
            raise HTTPFailure('本次登录会话已停止，不会重建或重试')
        if self.state.get('ready'):
            return
        if self.state.get('initializing'):
            raise HTTPFailure('上次登录初始化未完成，保留现场等待核查，不重新创建会话')
        self.state['initializing'] = True
        self.save()
        self.api._prepare_login_session()
        self.api._complete_security()
        self.state['ready'] = True
        self.state.pop('initializing', None)

    def start_login(self, output: Path):
        self.prepare()
        success, _, data = self.api.generate_qrcode()
        if not success or not data or not data.get('qr_id'):
            raise HTTPFailure('小红书没有发放有效二维码，已停止')
        link = data.get('qr_url', '')
        self.state['qr_id'] = data['qr_id']
        write_qr(link, output)
        return {'status': 'waiting', 'qr_image': str(output)}

    def accept(self):
        success, user, _ = self.api.get_user_info()
        if not success or not user:
            raise HTTPFailure('小红书尚未确认有效的创作者登录态')
        self.state.update(authenticated=True)
        self.state.pop('phone', None)
        self.state.pop('qr_id', None)
        return {'status': 'authenticated', 'message': '小红书 HTTP 登录成功'}

    def poll_login(self):
        if not self.state.get('qr_id'):
            raise ValueError('请先生成小红书二维码')
        data = self.api.query_qrcode_status(self.state['qr_id'])
        if data.get('success') is True and data.get('status') == 1:
            return self.accept()
        status = {2: 'waiting', 3: 'waiting_confirmation', 4: 'expired'}.get(data.get('status'))
        if status is None:
            raise HTTPFailure('小红书扫码状态异常，已停止')
        return {'status': status}

    def send_sms(self, phone: str):
        if not re.fullmatch(r'1[3-9][0-9]{9}', phone):
            raise ValueError('请输入有效的中国大陆手机号')
        if self.state.get('sms_requested'):
            raise HTTPFailure('本会话已请求过短信，不会重复发送')
        self.prepare()
        self.state.update(sms_requested=True, phone=phone)
        self.save()  # Mark before network: uncertain delivery must not cause a resend.
        success, _, _ = self.api.send_phone_code(phone)
        if not success:
            raise HTTPFailure('小红书未确认短信发送成功，已停止')
        return {'status': 'waiting_sms', 'message': '短信接口已接受请求，请提供收到的验证码'}

    def confirm_sms(self, code: str):
        if not self.state.get('sms_requested') or not self.state.get('phone'):
            raise ValueError('请先请求小红书短信验证码')
        if not re.fullmatch(r'[0-9]{6}', code):
            raise ValueError('请输入 6 位验证码')
        success, _, _ = self.api.login_by_phone(self.state['phone'], code)
        if not success:
            raise HTTPFailure('小红书未接受验证码，已停止')
        return self.accept()

    def export(self, path: Path):
        if not self.state.get('authenticated'):
            raise ValueError('尚未验证小红书账号，不导出发布凭证')
        import datetime
        now = datetime.datetime.now()
        private_json(path, {'cookie': self.api.profile.cookies, 'profile': dump_profile(self.api.profile),
                            'signing': {'host_cookie_state': self.api.host_cookie_state()},
                            'expires_at': (now + datetime.timedelta(days=365)).strftime('%Y-%m-%d %H:%M:%S'),
                            'updated_at': now.strftime('%Y-%m-%d %H:%M:%S')})


class XHSPCLogin(XHSLoginRuntime):
    """PC QR flow: persisted device, non-guest validation, atomic creator export."""

    def __init__(self, path: Path, *, source: Path):
        super().__init__(path, source=source)
        from apis.xhs_pc_login_apis import XHSLoginApi
        from xhs_utils.xhs_pc.http import PcHttpClient
        self.api = XHSLoginApi(http_client=PCLoginTransport(PcHttpClient()))

    @classmethod
    def load(cls, path: Path, *, source: Path):
        if path.stat().st_mode & 0o077:
            raise ValueError('登录会话文件须为 600 权限')
        data = json.loads(path.read_text())
        client = cls(path, source=source)
        from xhs_utils.xhs_core.cookies import HostCookieStore
        if data.get('profile'):
            client.api.profile = restore_pc_profile(data['profile'])
        client.api._cookie_store = HostCookieStore.from_state(data['host_cookie_state'])
        client.api.dsl = data.get('dsl', '')
        client.api._login_b1 = data.get('login_b1', '')
        client.api._webprofile_reported = data.get('webprofile_reported', False)
        client.api.http.blocked = data.get('blocked', False)
        client.api.http.last_status = data.get('last_response', {}).get('http_status')
        client.api.http.last_path = data.get('last_response', {}).get('path', '')
        client.state = data.get('login', {'ready': bool(client.api.profile and client.api._webprofile_reported),
                                        'resume_account': True})
        return client

    def save(self):
        private_json(self.path, {
            'version': 1, 'profile': dump_profile(self.api.profile) if self.api.profile else None,
            'host_cookie_state': self.api.host_cookie_state(), 'dsl': self.api.dsl,
            'login_b1': self.api._login_b1, 'webprofile_reported': self.api._webprofile_reported,
            'login': self.state, 'blocked': self.api.http.blocked,
            'last_response': {'http_status': self.api.http.last_status, 'path': self.api.http.last_path},
        })

    def prepare(self):
        if self.api.http.blocked:
            raise HTTPFailure('本次小红书主站登录会话已停止，不自动重试')
        if self.state.get('ready'):
            return
        if self.state.get('initializing'):
            raise HTTPFailure('上次初始化未完成，保留设备现场，不自动重新初始化')
        self.state['initializing'] = True
        self.save()
        cookies = self.api.generate_init_cookies()
        self.api.ensure_webprofile(cookies)
        self.state['ready'] = True
        self.state.pop('initializing', None)
        self.save()

    def accept(self):
        ok, user, _ = self.api.get_user_info(self.api.profile.cookie_map)
        if not ok or user.get('guest') is not False or not user.get('user_id'):
            self.state.pop('authenticated', None)
            raise HTTPFailure('主站未确认正式账号；游客会话不能用于投稿')
        self.state.update(authenticated=True, account_id=str(user['user_id']))
        for key in ('resume_account', 'qr_id', 'qr_code', 'qr_url', 'qr_expired'):
            self.state.pop(key, None)
        return {'status': 'authenticated', 'message': '小红书主站 HTTP 登录已确认'}

    def start_login(self, output: Path, *, refresh=False):
        self.prepare()
        if not refresh and (self.state.get('authenticated') or self.state.get('resume_account')):
            return self.accept()
        if self.state.get('qr_expired') and not refresh:
            raise HTTPFailure('二维码已过期；使用 --refresh 明确申请新二维码')
        if refresh or not self.state.get('qr_id'):
            for key in ('authenticated', 'account_id', 'resume_account'):
                self.state.pop(key, None)
            ok, _, data = self.api.generate_qrcode(self.api.profile.cookie_map)
            if not ok or not data or not data.get('qr_id') or not data.get('code'):
                raise HTTPFailure('小红书没有发放有效主站二维码')
            self.state.update(qr_id=data['qr_id'], qr_code=data['code'], qr_url=data['qr_url'])
            self.state.pop('qr_expired', None)
            self.save()
        write_qr(self.state['qr_url'], output)
        return {'status': 'waiting', 'qr_image': str(output)}

    def poll_login(self):
        if self.state.get('authenticated'):
            return self.accept()
        if not self.state.get('qr_id') or not self.state.get('qr_code'):
            raise ValueError('请先生成小红书主站二维码')
        ok, message, _ = self.api.check_qrcode_status(
            self.state['qr_id'], self.state['qr_code'], self.api.profile.cookie_map)
        if ok:
            return self.accept()
        status = {'请扫描二维码': 'waiting', '请确认登录': 'waiting_confirmation', '二维码已过期': 'expired'}.get(message)
        if status is None:
            raise HTTPFailure('小红书扫码状态异常，已停止')
        self.state['qr_expired'] = status == 'expired'
        return {'status': status}

    def export(self, path: Path):
        if not self.state.get('authenticated'):
            raise ValueError('主站账号尚未验证，不导出凭证')
        from .xhs_http import XHSHTTP
        from xhs_utils.xhs_pc.state import cookie_header
        import datetime
        now = datetime.datetime.now()
        cookies = self.api.profile.cookie_map
        config = {'cookie': cookie_header(cookies), 'signing': {'host_cookie_state': self.api.host_cookie_state()},
                  'expires_at': (now + datetime.timedelta(days=365)).strftime('%Y-%m-%d %H:%M:%S'),
                  'updated_at': now.strftime('%Y-%m-%d %H:%M:%S')}
        # Retain the creator signing context only when it belongs to this device.
        if path.exists():
            if path.stat().st_mode & 0o077:
                raise ValueError('凭证文件须为 600 权限')
            old = json.loads(path.read_text())
            profile = old.get('profile')
            if profile and profile.get('cookies', {}).get('a1') == cookies.get('a1'):
                config['profile'] = profile
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix='.creator-candidate-', dir=path.parent)
        os.close(fd)
        candidate, client = Path(name), None
        try:
            private_json(candidate, config)
            client = XHSHTTP(candidate, source=self.source)
            account = client.call('GET', '/api/galaxy/user/info', cold=True).get('data') or {}
            if not account.get('userId') or str(account['userId']) != self.state.get('account_id'):
                raise HTTPFailure('创作者账号身份不匹配，保留原有发布凭证')
            client.save()
            private_json(path, json.loads(candidate.read_text()))
        except HTTPFailure:
            self.api.http.blocked = True
            raise
        finally:
            if client:
                client.auth.close()
            candidate.unlink(missing_ok=True)
