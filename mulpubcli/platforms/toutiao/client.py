"""Toutiao creator HTTP adapter. No browser, retry loop or cached request signatures."""
from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import time
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from PIL import Image

from mulpubcli.core import Article, PublishResult, content_fingerprint, content_matches, strip_markdown_images
from mulpubcli.http import HTTP, HTTPFailure, iter_cookies, load_session, save_session
from mulpubcli.renderer import render as _render_article, _ArticleHTML


PLATFORM = 'toutiao'
MP = 'https://mp.toutiao.com'
SSO = 'https://sso.toutiao.com'
QR_PARAMS = {'service': MP + '/profile_v4/', 'need_logo': 'true', 'ui_version': '3.3.2',
             'aid': '1231', 'account_sdk_source': 'sso', 'sdk_version': '2.2.6', 'language': 'zh'}


def identifier(value):
    # Never accept booleans, floats or exponent notation for a 64-bit platform ID.
    return str(value) if type(value) in (str, int) and re.fullmatch(r'[1-9][0-9]{0,24}', str(value)) else ''


def image_identity(url: str) -> str:
    if not isinstance(url, str):
        return ''
    parsed = urlsplit('https:' + url if url.startswith('//') else url)
    host = parsed.hostname or ''
    if (parsed.scheme != 'https' or parsed.username or parsed.password
            or not (host.endswith('.toutiaoimg.com') or host.endswith('.toutiao.com') or host.endswith('.pstatp.com'))):
        return ''
    # The upload URI survives CDN resize/template/query variations.
    match = re.search(r'(?:^|/)((?:pgc-image|tos-cn-i-[a-z0-9]+)/[A-Za-z0-9_-]+)(?:[~.]|$)', parsed.path)
    return match[1] if match else ''


def article_form(article: Article, image: dict, image_map: dict[str, str], *,
                 public: bool, media_id: str) -> dict:
    """Build the POST form for the Toutiao article publish endpoint.

    image      : cover image dict returned by upload_image(article.cover)
    image_map  : {local_path_str -> platform_url} for all images (cover + body)
    """
    cover = {key: image[key] for key in ('url', 'uri', 'width', 'height')}
    # Cover uses Toutiao's special pgc-img wrapper; body uses standard renderer
    cover_div = (f'<div class="pgc-img"><img src="{escape(image["url"], quote=True)}" '
                 f'img_width="{image["width"]}" img_height="{image["height"]}"></div>')
    body_html = _render_article(article, image_map, cover_first=False, include_title=False)
    html = cover_div + body_html
    extra = {'content_source': 100000000402, 'content_word_cnt': len(article.body),
             'is_multi_title': 0, 'sub_titles': [], 'tuwen_wtt_transfer_switch': '0'}
    return {'source': 29, 'title': article.title, 'content': html, 'pgc_id': '',
            'title_id': f'{int(time.time() * 1000)}_{media_id}',
            'extra': json.dumps(extra, ensure_ascii=False, separators=(',', ':')),
            'save': int(public), 'entrance': 'main' if public else '', 'timer_status': 0, 'timer_time': '',
            'article_type': 0, 'draft_form_data': '{"coverType":2}',
            'pgc_feed_covers': json.dumps([cover], ensure_ascii=False, separators=(',', ':')),
            'ic_uri_list': image['uri'], 'article_ad_type': 2, 'is_fans_article': 0,
            'claim_exclusive': 0, 'govern_forward': 0, 'praise': 0, 'disable_praise': 1,
            'tree_plan_article': 0, 'educluecard': '', 'activity_tag': 0, 'trends_writing_tag': '',
            'search_creation_info': json.dumps({'searchTopOne': 0, 'abstract': '', 'clue_id': ''}),
            'mp_editor_stat': '{}'}


class ToutiaoWeb:
    HOSTS = {'mp.toutiao.com', 'sso.toutiao.com'}

    def __init__(self, *, session=None, user_agent=None, network='direct'):
        if network not in ('direct', 'environment'):
            raise ValueError('头条网络模式须为 direct 或 environment')
        self.http = HTTP(self.HOSTS, session=session)
        if user_agent is not None:
            if not isinstance(user_agent, str) or any(c in user_agent for c in '\r\n'):
                raise ValueError('User-Agent 无效')
            self.http.session.headers['User-Agent'] = user_agent
        self.http.session.trust_env = network == 'environment'
        self.network = network
        self.account_id = self.user_id = self.qr_token = self.csrf_token = ''
        self.csrf_expires_at = 0
        self.qr_created_at = 0
        self.qr_confirmed = False
        self.login_blocked = False
        self.mobile_bound = False
        self.path = None
        self.http.on_response = self._response

    @classmethod
    def load(cls, path: Path):
        session, metadata = load_session(path, cls.HOSTS)
        client = cls(session=session, user_agent=metadata.get('user_agent'), network=metadata.get('network', 'direct'))
        for key in ('account_id', 'user_id', 'qr_token', 'csrf_token'):
            value = metadata.get(key, '')
            if not isinstance(value, str) or '\r' in value or '\n' in value:
                client.close()
                raise ValueError('头条会话元数据格式无效')
            setattr(client, key, value)
        for key in ('qr_created_at', 'csrf_expires_at'):
            value = metadata.get(key, 0)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                client.close()
                raise ValueError('头条会话时间戳无效')
            setattr(client, key, value)
        client.qr_confirmed = metadata.get('qr_confirmed') is True
        client.login_blocked = metadata.get('login_blocked', False) is True
        client.path = path
        return client

    def save(self, path: Path):
        save_session(path, self.http.session, user_agent=self.http.session.headers['User-Agent'],
                     network=self.network, account_id=self.account_id, user_id=self.user_id,
                     qr_token=self.qr_token, qr_created_at=self.qr_created_at,
                     csrf_token=self.csrf_token, csrf_expires_at=self.csrf_expires_at,
                     login_blocked=self.login_blocked, qr_confirmed=self.qr_confirmed)
        self.path = path

    def _persist(self):
        if self.path is not None:
            self.save(self.path)

    def _response(self, response):
        if urlsplit(response.url).hostname == 'mp.toutiao.com':
            parts = response.headers.get('x-ware-csrf-token', '').split(',')
            if len(parts) >= 3 and parts[0] == '0' and parts[1] and parts[2].isdigit():
                if not any(c in parts[1] for c in '\r\n'):
                    self.csrf_token = parts[1]
                    self.csrf_expires_at = time.time() + min(int(parts[2]) / 1000, 86400)
        self._persist()

    def close(self):
        self.http.session.close()

    def _guard(self):
        if self.login_blocked:
            raise HTTPFailure('头条会话已被拒绝，停止请求；需处理平台验证或导入新的有效会话', kind='verification_required')

    def _checked(self, payload, *, sso=False, mobile_rejection=False):
        key = 'error_code' if sso else 'code'
        if not isinstance(payload, dict) or type(payload.get(key)) is not int:
            raise HTTPFailure('头条响应缺少明确业务状态，停止操作', kind='invalid_response')
        code = payload[key]
        # 20100 是 mp feed/回读接口的已认证正常标记（见 find_article），绝不是拒绝，
        # 也不能据此锁定会话。错把它当拒绝会让已成功的发布误报"拒绝请求"并封禁整个会话。
        if not sso and code == 20100:
            return payload.get('data')
        if code != 0:
            data = payload.get('data')
            if mobile_rejection and code == 2103 and isinstance(data, dict) and type(data.get('pgc_id')) in (str, int) and str(data['pgc_id']) == '0':
                raise HTTPFailure('头条要求先绑定手机号（code=2103），未创建文章',
                                  kind='account_setup_required', code=code)
            self.login_blocked = True
            self._persist()
            kind = 'authentication_required' if code == 100004 else 'platform_rejected'
            if code == 2222:
                kind = 'verification_required'
            raise HTTPFailure(f'头条拒绝请求（code={code}），已停止且不重试', kind=kind, code=code)
        data = payload.get('data')
        if not isinstance(data, dict):
            raise HTTPFailure('头条 data 结构变化，停止操作', kind='invalid_response')
        return data

    def _reject(self, exc):
        if exc.kind in ('platform_rejected', 'verification_required', 'rate_limited', 'authentication_required'):
            self.login_blocked = True
            self._persist()

    def _raw_json(self, method, url, **kwargs):
        self._guard()
        try:
            return self.http.json(method, url, **kwargs)
        except HTTPFailure as exc:
            self._reject(exc)
            raise

    def _json(self, method, url, *, sso=False, **kwargs):
        self._guard()
        try:
            return self._checked(self._raw_json(method, url, **kwargs), sso=sso,
                mobile_rejection=method == 'POST' and urlsplit(url).hostname == 'mp.toutiao.com'
                and urlsplit(url).path == '/mp/agw/article/publish')
        except HTTPFailure as exc:
            self._reject(exc)
            raise

    def call(self, method, path, *, raw=False, **kwargs):
        headers = {'Referer': MP + '/profile_v4/graphic/publish', 'Origin': MP,
                   'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}
        if self.csrf_token:
            headers['x-secsdk-csrf-token'] = self.csrf_token
        headers.update(kwargs.pop('headers', {}))
        request = self._raw_json if raw else self._json
        return request(method, MP + path, headers=headers, **kwargs)

    def account(self):
        self._guard()
        if not any(c.name in ('sessionid', 'sessionid_ss', 'sid_tt') and c.value and not c.is_expired()
                   for c in iter_cookies(self.http.session)):
            raise HTTPFailure('头条尚无有效登录 Cookie，请先扫码或导入会话', kind='authentication_required')
        data = self.call('GET', '/mp/agw/media/user_login_status_api')
        if data.get('is_login') is not True:
            raise HTTPFailure('头条登录态已失效', kind='authentication_required')
        profile = self.call('GET', '/mp/agw/media/get_media_info')
        media, user = profile.get('media') or {}, profile.get('user') or {}
        if not isinstance(media, dict) or not isinstance(user, dict):
            raise HTTPFailure('头条账号详情结构无效', kind='invalid_response')
        owner = identifier(media.get('id_str') or media.get('id'))
        user_id = identifier(user.get('id_str') or user.get('id'))
        if not owner or not user_id or media.get('is_enable') is not True:
            raise HTTPFailure('头条未返回有效创作者身份，可能需要开通头条号', kind='authentication_required')
        if (self.account_id and owner != self.account_id) or (self.user_id and user_id != self.user_id):
            raise HTTPFailure('头条账号与保存身份不一致，已停止', kind='account_mismatch')
        status_user = data.get('user') or {}
        self.mobile_bound = (isinstance(status_user, dict) and status_user.get('mobile_bind_ok') is True
                             or media.get('has_avoid_bind_phone_permission') is True)
        self.account_id, self.user_id = owner, user_id
        self._persist()
        return {'id': owner, 'user_id': user_id}

    def start_login(self, output: Path, *, refresh=False):
        self._guard()
        if self.qr_token and not refresh:
            if time.time() - self.qr_created_at >= 120 or not output.is_file():
                # A stale QR is never useful; rotate it immediately while preserving
                # the current anonymous device/session cookies.
                self.qr_token = ''
                self.qr_created_at = 0
                refresh = True
            else:
                return {'status': 'waiting', 'qr_image': str(output)}
        data = self._json('GET', SSO + '/get_qrcode/', sso=True, params=QR_PARAMS)
        token, encoded = data.get('token'), data.get('qrcode')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', token):
            raise HTTPFailure('头条二维码 token 结构无效', kind='invalid_response')
        if not isinstance(encoded, str) or len(encoded) > 2_000_000 or not encoded.startswith('data:image/png;base64,'):
            raise HTTPFailure('头条二维码图像结构无效', kind='invalid_response')
        try:
            raw = base64.b64decode(encoded.split(',', 1)[1], validate=True)
            with Image.open(io.BytesIO(raw)) as image:
                image.verify()
        except (ValueError, OSError):
            raise HTTPFailure('头条二维码不是有效图像', kind='invalid_response') from None
        self.qr_token, self.qr_created_at = token, time.time()
        self.qr_confirmed = False
        self._persist()
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        return {'status': 'waiting', 'qr_image': str(output)}

    def _finish_login(self, target):
        self._guard()
        for _ in range(6):
            parsed = urlsplit(target) if isinstance(target, str) else None
            if (not parsed or parsed.scheme != 'https' or parsed.hostname not in self.HOSTS
                    or parsed.username or parsed.password or parsed.port not in (None, 443)):
                raise HTTPFailure('头条登录回跳地址未获允许', kind='invalid_response')
            try:
                response = self.http.request('GET', target, accepted_statuses=(302, 303, 307, 308))
            except HTTPFailure as exc:
                self._reject(exc)
                raise
            if response.status_code in (302, 303, 307, 308):
                target = urljoin(target, response.headers.get('Location', ''))
            else:
                return
        raise HTTPFailure('头条登录回跳次数超过上限', kind='invalid_response')

    def poll_login(self):
        self._guard()
        if self.qr_confirmed or (not self.qr_token and self.account_id and self.user_id):
            self.account()
            self.qr_token = ''
            self._persist()
            if not self.mobile_bound:
                return {'status': 'missing_phone', 'message': '扫码成功，但头条号未绑定手机号，请在 App 或网页端完成手机号绑定'}
            return {'status': 'authenticated', 'message': '已恢复待保存的头条登录会话'}
        if not self.qr_token:
            raise ValueError('请先生成头条二维码')
        data = self._json('GET', SSO + '/check_qrconnect/', sso=True, params={**QR_PARAMS, 'token': self.qr_token})
        status = str(data.get('status', ''))
        if status in ('1', 'new', '2', 'scanned'):
            return {'status': 'scanned' if status in ('2', 'scanned') else 'waiting', 'message': '等待今日头条 App 确认登录'}
        if status in ('4', '5', 'expired'):
            return {'status': 'expired', 'message': '二维码失效，请显式刷新；不会自动换码'}
        if status not in ('3', 'confirmed'):
            raise HTTPFailure('头条扫码返回未知状态，停止操作', kind='invalid_response')
        self._finish_login(data.get('redirect_url'))
        self.qr_confirmed = True
        self._persist()
        self.account()
        self.qr_token = ''
        self._persist()
        if not self.mobile_bound:
            return {'status': 'missing_phone', 'message': '扫码成功，但头条号未绑定手机号，请在 App 或网页端完成手机号绑定'}
        return {'status': 'authenticated', 'message': '头条扫码登录及创作者身份核验成功'}

    def prepare_csrf(self):
        self._guard()
        if self.csrf_token and self.csrf_expires_at > time.time() + 30:
            return
        self.csrf_token, self.csrf_expires_at = '', 0
        try:
            self.http.request('HEAD', MP + '/spice/image', headers={
                'x-secsdk-csrf-request': '1', 'x-secsdk-csrf-version': '1.2.22'})
        except HTTPFailure as exc:
            self._reject(exc)
            raise
        if not self.csrf_token or self.csrf_expires_at <= time.time() + 30:
            raise HTTPFailure('未取得有效 CSRF 凭证，停止写入', kind='invalid_response')

    def upload_image(self, path: Path):
        if not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError('头条配图须存在且不超过 10MB')
        with Image.open(path) as image:
            if image.format not in ('PNG', 'JPEG', 'WEBP'):
                raise ValueError('头条配图须为 PNG、JPEG 或 WEBP')
            width, height = image.size
            mime = Image.MIME[image.format]
            image.verify()
        self.prepare_csrf()
        with path.open('rb') as handle:
            data = self.call('POST', '/spice/image', params={'upload_source': '20020003', 'aid': '1231',
                'device_platform': 'web', 'need_cover_url': '1'}, files={'image': (path.name, handle, mime)})
        uri = data.get('origin_image_uri') or data.get('image_uri')
        url = data.get('origin_image_url') or data.get('image_url')
        if not isinstance(uri, str) or not uri or image_identity(url) != uri:
            raise HTTPFailure('头条上传响应的图片身份或地址无效', kind='invalid_response')
        return {'uri': uri, 'url': url, 'width': data.get('image_width') or width, 'height': data.get('image_height') or height}

    def detail(self, article_id):
        if not identifier(article_id):
            raise ValueError('头条文章 ID 无效')
        payload = self.call('GET', '/mp/agw/article/edit', raw=True,
                            params={'pgc_id': article_id, 'wxstyle': 0, 'format': 'json'})
        # Current editor consumes article_pgc at the root; errors still use code/data.
        if 'code' in payload:
            data = self._checked(payload)
        elif isinstance(payload.get('article_pgc'), dict):
            data = payload
        else:
            raise HTTPFailure('头条文章详情结构待核对，停止核验', kind='invalid_response')
        pgc = data.get('article_pgc') or {}
        return {**data, 'media_id': data.get('media_id') or pgc.get('media_id')}

    @staticmethod
    def _snake_keys(value):
        if isinstance(value, list):
            return [ToutiaoWeb._snake_keys(item) for item in value]
        if not isinstance(value, dict):
            return value
        return {re.sub(r'([A-Z])', r'_\1', key).lower().lstrip('_'): ToutiaoWeb._snake_keys(item)
                for key, item in value.items()}

    def find_article(self, article_id, *, draft=False):
        # From the current graphic editor articles chunk. Bounded to 100 recent entries.
        for page in range(1, 6):
            params = {'provider_type': 'mp_provider', 'aid': '13', 'app_name': 'news_article',
                'category': 'mp_article', 'channel': '', 'stream_api_version': '88',
                'genre_type_switch': json.dumps({'repost': 1, 'small_video': 1, 'toutiao_graphic': 1,
                    'weitoutiao': 1, 'xigua_video': 1}), 'device_platform': 'pc', 'platform_id': '0',
                'visited_uid': self.user_id, 'offset': str((page - 1) * 20), 'count': '20', 'keyword': '',
                'client_extra_params': json.dumps({'category': 'mp_article', 'real_app_id': '1231',
                    'need_forward': 'true', 'offset_mode': '1', 'page_index': str(page),
                    'status': '9' if draft else '0', 'source': '0'})}
            payload = self.call('GET', '/api/feed/mp_provider/v1/', raw=True, params=params,
                headers={'Content-Type': 'application/json', 'RPC-PERSIST-BYTETIM_BUSINESS_STREAM_CALLER': 'mp'})
            if 'code' in payload:
                self._checked(payload)  # Recognize/latch conventional error envelopes too.
            # Observed authenticated feed envelope is errno=20100, login_status=1,
            # message="success" regardless of whether data is empty or populated.
            # That exact shape is accepted as a normal authenticated read, never a rejection.
            benign = (payload.get('errno') == 20100 and payload.get('login_status') == 1
                      and payload.get('message') == 'success')
            if not benign:
                info = payload.get('api_base_info') or {}
                code = payload.get('errno', info.get('status_code'))
                self._checked({'code': code, 'data': {}})
            items = payload.get('data')
            if not isinstance(items, list) or type(payload.get('has_more')) is not bool:
                raise HTTPFailure('头条作品列表结构待核对，停止核验', kind='invalid_response')
            for item in items:
                try:
                    encoded = item.get('content') or item['assembleCell']['itemCell']['extra']['origin_content']
                    content = self._snake_keys(json.loads(encoded))
                    attr = content['article_attr']
                    cell = self._snake_keys(json.loads(attr['pgc_cell']))
                    pgc_id = identifier(cell.get('pgc_id'))
                    if pgc_id == article_id:
                        return {**cell, 'pgc_id': pgc_id, 'status': attr['status']}
                except (KeyError, TypeError, ValueError, AttributeError):
                    raise HTTPFailure('头条作品列表条目结构待核对，停止核验', kind='invalid_response') from None
            if not payload['has_more']:
                break
        return None

    def list_articles(self, *, draft=False) -> list[dict]:
        # Reuses the same graphic-editor articles feed pagination as find_article,
        # but collects every entry instead of stopping at the first id match.
        found: list[dict] = []
        for page in range(1, 6):
            params = {'provider_type': 'mp_provider', 'aid': '13', 'app_name': 'news_article',
                'category': 'mp_article', 'channel': '', 'stream_api_version': '88',
                'genre_type_switch': json.dumps({'repost': 1, 'small_video': 1, 'toutiao_graphic': 1,
                    'weitoutiao': 1, 'xigua_video': 1}), 'device_platform': 'pc', 'platform_id': '0',
                'visited_uid': self.user_id, 'offset': str((page - 1) * 20), 'count': '20', 'keyword': '',
                'client_extra_params': json.dumps({'category': 'mp_article', 'real_app_id': '1231',
                    'need_forward': 'true', 'offset_mode': '1', 'page_index': str(page),
                    'status': '9' if draft else '0', 'source': '0'})}
            payload = self.call('GET', '/api/feed/mp_provider/v1/', raw=True, params=params,
                headers={'Content-Type': 'application/json', 'RPC-PERSIST-BYTETIM_BUSINESS_STREAM_CALLER': 'mp'})
            if 'code' in payload:
                self._checked(payload)  # Recognize/latch conventional error envelopes too.
            # Same benign 20100 authenticated-feed envelope tolerated in find_article.
            benign = (payload.get('errno') == 20100 and payload.get('login_status') == 1
                      and payload.get('message') == 'success')
            if not benign:
                info = payload.get('api_base_info') or {}
                code = payload.get('errno', info.get('status_code'))
                self._checked({'code': code, 'data': {}})
            items = payload.get('data')
            if not isinstance(items, list) or type(payload.get('has_more')) is not bool:
                raise HTTPFailure('头条作品列表结构待核对，停止核验', kind='invalid_response')
            for item in items:
                try:
                    encoded = item.get('content') or item['assembleCell']['itemCell']['extra']['origin_content']
                    content = self._snake_keys(json.loads(encoded))
                    attr = content['article_attr']
                    cell = self._snake_keys(json.loads(attr['pgc_cell']))
                    pgc_id = identifier(cell.get('pgc_id'))
                    if not pgc_id:
                        raise HTTPFailure('头条作品列表条目结构待核对，停止核验', kind='invalid_response')
                    title = item.get('title') or cell.get('title') or ''
                    if not isinstance(title, str):
                        raise HTTPFailure('头条作品列表条目结构待核对，停止核验', kind='invalid_response')
                    item_id = identifier(cell.get('item_id')) or None
                    created = attr.get('create_time')
                    published_at = None
                    if isinstance(created, (int, float)) and created:
                        published_at = datetime.fromtimestamp(created).strftime('%Y-%m-%d %H:%M')
                    found.append({'id': pgc_id, 'title': title, 'status': attr['status'],
                                  'item_id': item_id, 'published_at': published_at})
                except (KeyError, TypeError, ValueError, AttributeError):
                    raise HTTPFailure('头条作品列表条目结构待核对，停止核验', kind='invalid_response') from None
            if not payload['has_more']:
                break
        return found

    def verify(self, article_id: str, *, expected=None, evidence=None, draft=False):
        if not identifier(article_id):
            raise ValueError('头条文章 ID 无效')
        self.account()
        proof = dict(evidence or {})
        if expected is not None:
            # 渲染后正文只保留可见文本与独立配图，markdown 图片语法不进入内容；
            # 与小红书一致，指纹须基于剥离图片语法后的正文计算。
            proof.update(content_fingerprint(PLATFORM, expected.title, strip_markdown_images(expected.body)))
        item = self.find_article(article_id, draft=draft)
        if not item:
            return PublishResult('pending', '已检查的作品列表未找到该文章，不会重发', platform=PLATFORM, verification='unavailable')
        status = item.get('status')
        if type(status) is not int or status != (9 if draft else 2):
            failed = status == 3 and type(status) is int
            return PublishResult('failed' if failed else 'pending', '头条尚未通过要求的发布状态核验', platform=PLATFORM,
                                 verification='mismatch' if status in (3, 4, 9) else 'unavailable')
        item_id = identifier(item.get('item_id'))
        url = f'https://www.toutiao.com/article/{item_id}/' if item_id else None
        if not proof.get('sha256') or not proof.get('media'):
            # 平台已返回发布状态，此时状态本身就是发布的确证；本地缺原稿/配图
            # 证据只意味着不做内容比对，仍应确认发布状态并回填公开链接。
            if draft:
                return PublishResult('draft', f'头条草稿 {article_id} 已保存', platform=PLATFORM, verification='published')
            if not item_id:
                return PublishResult('pending', '缺少公开 item_id，不能以草稿 ID 拼接公开链接', platform=PLATFORM, verification='unavailable')
            return PublishResult('published', '头条已发布；缺少原稿或上传图片证据，未做内容比对', url, PLATFORM, 'published')
        data = self.detail(article_id)
        html = _ArticleHTML(data.get('content'))
        covers = data.get('pgc_feed_covers')
        if isinstance(covers, str):
            try: covers = json.loads(covers)
            except ValueError: covers = None
        cover_ids = [v.get('uri') for v in covers if isinstance(v, dict)] if isinstance(covers, list) else []
        if (identifier(data.get('pgc_id')) != article_id or identifier(data.get('media_id')) != self.account_id
                or not content_matches(PLATFORM, data.get('title', ''), html.text, proof)
                or [image_identity(src) for src in html.images] != proof['media']
                or cover_ids != proof['media'][:1]):
            return PublishResult('pending', '头条标题、完整正文、配图、封面或账号未通过核对', platform=PLATFORM, verification='mismatch')
        if draft:
            return PublishResult('draft', f'头条草稿 {article_id} 完整回读通过', platform=PLATFORM, verification='verified')
        if not item_id:
            return PublishResult('pending', '缺少公开 item_id，不能以草稿 ID 拼接公开链接', platform=PLATFORM, verification='unavailable')
        return PublishResult('published', '头条已发表且原稿、配图与封面均回读一致；未验证其他账号展示', url, PLATFORM, 'verified')

    def publish(self, article: Article, *, checkpoint=None, public=True):
        if not 2 <= len(article.title) <= 30 or not article.body.strip() or len(article.body.encode()) > 1_000_000:
            return PublishResult('failed', '头条标题须为 2～30 字、正文非空且不超过本地 1MB 上限；不会自动截断', platform=PLATFORM)
        try:
            self.account()
        except Exception as exc:
            return PublishResult('failed', f'头条账号预检停止：{type(exc).__name__}；尚未上传或投稿', platform=PLATFORM)
        if not self.mobile_bound:
            return PublishResult('failed', '头条要求先绑定手机号；尚未上传或投稿', platform=PLATFORM)
        try:
            image = self.upload_image(article.cover)
        except Exception as exc:
            return PublishResult('failed', f'头条封面上传停止：{type(exc).__name__}；尚未投稿', platform=PLATFORM)

        # ── Upload body images ──────────────────────────────────────────────
        image_map: dict[str, str] = {str(article.cover): image['url']}
        media_uris: list[str] = [image['uri']]  # cover first, then body in document order
        for img_path in article.body_images:
            try:
                body_img = self.upload_image(img_path)
                image_map[str(img_path)] = body_img['url']
                media_uris.append(body_img['uri'])
            except Exception as exc:
                return PublishResult('failed',
                    f'头条正文图片上传停止（{img_path.name}）：{type(exc).__name__}；尚未投稿',
                    platform=PLATFORM)

        article_id = ''
        try:
            if checkpoint:
                checkpoint('uploaded', image['uri'])
            self.prepare_csrf()
            data = self.call('POST', '/mp/agw/article/publish', params={
                'source': 'mp', 'type': 'article', 'aid': '1231', 'mp_publish_ab_val': '0'},
                data=article_form(article, image, image_map, public=public, media_id=self.account_id))
            article_id = identifier(data.get('pgc_id') or data.get('pgcId'))
            if not article_id:
                return PublishResult('pending', '头条未返回有效文章 ID；请核验，不会重发', platform=PLATFORM)
            if checkpoint:
                checkpoint('submitted' if public else 'draft', article_id)
            return self.verify(article_id, expected=article, evidence={'media': media_uris}, draft=not public)
        except HTTPFailure as exc:
            if exc.kind == 'account_setup_required' and not article_id:
                return PublishResult('failed', str(exc), platform=PLATFORM)
            return PublishResult('pending', f'头条写入或回读结果待核验：{exc}；不会重发', platform=PLATFORM)
        except Exception as exc:
            return PublishResult('pending', f'头条写入或回读结果待核验：{type(exc).__name__}；不会重发', platform=PLATFORM)
