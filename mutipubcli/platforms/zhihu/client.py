"""Cookie-based Zhihu article workflow; no replay of expired signing headers."""
from __future__ import annotations

import hashlib
import json
import re
import time
import os
import uuid
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image

from mutipubcli.core import Article, PublishResult, content_fingerprint, content_matches
from mutipubcli.http import HTTP, HTTPFailure, iter_cookies, load_session, save_session
from mutipubcli.renderer import render as _render_article
from . import signing as zhihu_signing


class _ArticleHTML(HTMLParser):
    """Compare visible text and image sources while tolerating editor-added attributes."""
    BLOCKS = {'p', 'div', 'figure', 'br', 'li', 'h1', 'h2', 'h3', 'blockquote'}

    def __init__(self, content):
        super().__init__(convert_charrefs=True)
        self.parts, self.images = [], []
        if not isinstance(content, str):
            raise HTTPFailure('知乎正文响应结构无效', kind='invalid_response')
        self.feed(content)
        self.close()

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCKS:
            self.parts.append(' ')
        if tag == 'img':
            source = dict(attrs).get('src') or ''
            if source.startswith('//'):
                source = 'https:' + source
            self.images.append(urlsplit(source)._replace(query='', fragment='').geturl())

    def handle_endtag(self, tag):
        if tag in self.BLOCKS:
            self.parts.append(' ')

    @property
    def text(self):
        return ' '.join(''.join(self.parts).split())


class ZhihuWeb:
    MAIN = 'https://www.zhihu.com'
    COLUMN = 'https://zhuanlan.zhihu.com'
    HOSTS = {'www.zhihu.com', 'zhuanlan.zhihu.com', 'api.zhihu.com'}
    USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36'

    def __init__(self, *, session=None, user_agent=None, network='direct'):
        user_agent = user_agent or self.USER_AGENT
        if not isinstance(user_agent, str) or any(c in user_agent for c in '\r\n'):
            raise ValueError('User-Agent 无效')
        if network not in ('direct', 'environment'):
            raise ValueError('知乎网络模式须为 direct 或 environment')
        self.network, self.account_id = network, ''
        self.http = HTTP(self.HOSTS, session=session)
        self.http.session.trust_env = network == 'environment'
        self.http.session.headers.update({'User-Agent': user_agent, 'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9', 'Referer': self.COLUMN + '/', 'Origin': self.COLUMN, 'x-requested-with': 'fetch'})
        xsrf = [cookie.value for cookie in iter_cookies(self.http.session)
                if cookie.name == '_xsrf' and cookie.value and not cookie.is_expired()]
        if xsrf:
            self.http.session.headers['x-xsrftoken'] = xsrf[-1]
        self.qr_token = ''
        self.qr_link = ''
        self.qr_expires_at = 0
        self.login_blocked = False

    @classmethod
    def load(cls, path: Path):
        session, metadata = load_session(path, cls.HOSTS)
        client = cls(session=session, user_agent=metadata.get('user_agent'), network=metadata.get('network', metadata.get('transport', 'direct')))
        client.account_id = metadata.get('account_id', '')
        client.qr_token = metadata.get('qr_token', '')
        client.qr_link = metadata.get('qr_link', '')
        expiry = metadata.get('qr_expires_at', 0)
        client.qr_expires_at = expiry if type(expiry) is int and expiry > 0 else 0
        client.login_blocked = metadata.get('login_blocked', False)
        if client.qr_token:
            client._login_headers()
        client.http.on_response = lambda response: client.save(path)
        return client

    def save(self, path: Path):
        save_session(path, self.http.session, qr_token=self.qr_token, qr_link=self.qr_link,
                     qr_expires_at=self.qr_expires_at, login_blocked=self.login_blocked,
                     user_agent=self.http.session.headers['User-Agent'], network=self.network, account_id=self.account_id)

    def close(self):
        self.http.session.close()

    def _login_headers(self):
        self.http.session.headers.update({'Referer': self.MAIN + '/signin', 'Origin': self.MAIN})

    def start_login(self, output: Path, *, refresh=False):
        # This file belongs to the pending login. Never leave an unusable old QR visible.
        output.unlink(missing_ok=True)
        try:
            return self._start_login(output, refresh=refresh)
        except HTTPFailure:
            self.login_blocked = True
            raise

    def _start_login(self, output: Path, *, refresh=False):
        import qrcode
        if self.login_blocked:
            raise HTTPFailure('知乎登录会话被平台拒绝，请先处理平台验证；不会自动重试')
        self._login_headers()
        if not self.qr_token or refresh:
            self.http.request('GET', self.MAIN + '/signin', headers={
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'})
            self.http.request('POST', self.MAIN + '/udid')
            xsrf = [cookie.value for cookie in iter_cookies(self.http.session)
                    if cookie.name == '_xsrf' and cookie.value and not cookie.is_expired()]
            if not xsrf:
                raise HTTPFailure('知乎未授予登录 CSRF 会话')
            self.http.session.headers['x-xsrftoken'] = xsrf[-1]
            data = self.http.json('POST', self.MAIN + '/api/v3/account/api/login/qrcode')
            token, link, expiry = data.get('token'), data.get('link'), data.get('expires_at')
            if (not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', token)
                    or not isinstance(link, str) or type(expiry) is not int or expiry <= time.time()):
                raise HTTPFailure('知乎未返回有效二维码或有效期', kind='invalid_response')
            self.qr_token, self.qr_link, self.qr_expires_at = token, link, expiry
        # A successful create response alone does not establish a usable login flow.
        result = self.poll_login()
        if result['status'] != 'waiting':
            return result
        try:
            parsed = urlsplit(self.qr_link)
            valid_link = (parsed.scheme == 'https' and parsed.hostname == 'www.zhihu.com'
                          and not parsed.username and not parsed.password and parsed.port in (None, 443)
                          and parsed.path == '/account/scan/login/' + self.qr_token)
        except ValueError:
            valid_link = False
        if not valid_link:
            raise HTTPFailure('知乎二维码地址无效', kind='invalid_response')
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as target:
            qrcode.make(self.qr_link).save(target, format='PNG')
        if self.qr_expires_at <= time.time():
            output.unlink(missing_ok=True)
            return {'status': 'expired', 'message': '生成图片时二维码已过期，请显式使用 --refresh'}
        return dict(result, qr_image=str(output), expires_at=self.qr_expires_at)

    def poll_login(self):
        if self.login_blocked:
            raise HTTPFailure('知乎登录会话被平台拒绝，不再重复查询')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', self.qr_token):
            raise ValueError('请先生成知乎登录二维码')
        if self.qr_expires_at <= time.time():
            return {'status': 'expired', 'message': '知乎二维码已过期或缺少有效期，请显式使用 --refresh；不会自动换码'}
        try:
            data = self.http.json('GET', self.MAIN + f'/api/v3/account/api/login/qrcode/{self.qr_token}/scan_info', headers={
                'Referer': self.MAIN + '/signin', 'Accept': '*/*'})
            HTTP.checked(data)
            if data.get('access_token'):
                self.account()
                self.qr_token, self.qr_link, self.qr_expires_at = '', '', 0
                self.http.session.headers.update({'Referer': self.COLUMN + '/', 'Origin': self.COLUMN})
                return {'status': 'authenticated', 'message': '知乎 HTTP 登录成功'}
            if data.get('new_token'):
                self.qr_expires_at = 0
                return {'status': 'expired', 'message': '平台已使旧二维码失效，请显式使用 --refresh'}
            if type(data.get('status')) is not int or data['status'] not in (0, 1):
                raise HTTPFailure('知乎扫码响应状态未知，已停止', kind='invalid_response')
            if self.qr_expires_at <= time.time():
                return {'status': 'expired', 'message': '查询期间二维码已过期，请显式使用 --refresh'}
        except HTTPFailure:
            self.login_blocked = True
            raise
        return {'status': 'waiting', 'message': '等待知乎 App 扫码确认'}

    def account(self):
        if not any(cookie.name == 'z_c0' and cookie.value and not cookie.is_expired()
                   for cookie in iter_cookies(self.http.session)):
            raise HTTPFailure('知乎尚无有效登录 Cookie；扫码确认后仍须取得网站登录态', kind='authentication_required')
        result = self.http.json('GET', self.MAIN + '/api/v4/me')
        HTTP.checked(result)
        if not isinstance(result.get('id'), str) or not result['id']:
            raise HTTPFailure('知乎登录态无效')
        if self.account_id and self.account_id != result['id']:
            raise HTTPFailure('知乎返回的账号与保存的账号不同，已停止', kind='account_mismatch')
        self.account_id = result['id']
        return {key: result.get(key) for key in ('id', 'name')}

    def upload_image(self, path: Path):
        with Image.open(path) as image:
            mime = Image.MIME[image.format]
            image.verify()
        data = path.read_bytes()
        result = self.http.json('POST', 'https://api.zhihu.com/images', json={
            'image_hash': hashlib.md5(data).hexdigest(), 'source': 'article',
        })
        upload = result['upload_file']
        image_id = str(upload['image_id'])
        if not re.fullmatch(r'[A-Za-z0-9_-]+', image_id):
            raise HTTPFailure('知乎图片标识无效')
        if upload.get('state') != 1:
            token = result['upload_token']
            ZhihuAPI.upload_oss({'endpoint': 'https://zhihu-pics-upload.zhimg.com', 'is_cname': True,
                                 'bucket_name': 'zhihu-pics', 'object_name': upload['object_key']},
                                {'access_key_id': token['access_id'], 'secret_access_key': token['access_key'],
                                 'security_token': token['access_token']}, data, mime)
            # The website acknowledges OSS completion before processing starts.
            # This endpoint returns HTTP 200 with an empty body.
            self.http.request('PUT', 'https://api.zhihu.com/images/' + image_id + '/uploading_status',
                              json={'upload_result': 'success'})
        for attempt in range(15):
            detail = self.http.json('GET', 'https://api.zhihu.com/images/' + image_id)
            key = detail.get('original_hash', '')
            source = detail.get('original_src', '')
            if detail.get('status') == 'success':
                if (isinstance(key, str) and key and re.fullmatch(r'[A-Za-z0-9_.-]+', key)
                        and self._image_key(source) == key):
                    return source
                raise HTTPFailure('知乎图片结果缺少匹配的原图地址', kind='invalid_response')
            if detail.get('status') not in ('init', 'processing'):
                raise HTTPFailure('知乎图片处理失败或状态未知', kind='invalid_response')
            if attempt < 14:
                time.sleep(2)
        raise HTTPFailure('知乎图片尚未处理完成')

    def create_draft(self, article: Article, *, checkpoint=None, media_checkpoint=None, _account_checked=False):
        if not _account_checked:
            self.account()
        image = self.upload_image(article.cover)
        if media_checkpoint:
            media_checkpoint(self._image_key(image))
        draft = self.http.json('POST', self.COLUMN + '/api/articles/drafts', json={
            'title': article.title, 'content': '', 'delta_time': 0,
        })
        draft_id = str(draft.get('id', ''))
        if not draft_id.isdigit():
            raise HTTPFailure('知乎未返回草稿 ID，结果需要核验')
        if checkpoint:
            checkpoint(draft_id)
        # ── Upload cover image ──────────────────────────────────────────────
        cover_url = image
        # ── Upload body images (if any local images in body) ────────────────
        image_map: dict[str, str] = {str(article.cover): cover_url}
        for img_path in article.body_images:
            body_img_url = self.upload_image(img_path)
            image_map[str(img_path)] = body_img_url
        # ── Render HTML (cover inline at top, then body) ────────────────────
        html = _render_article(article, image_map, cover_first=True, include_title=False)
        self.http.request('PATCH', self.COLUMN + f'/api/articles/{draft_id}/draft', json={
            'title': article.title, 'content': html, 'delta_time': 0,
        })
        saved = self.http.json('GET', self.COLUMN + f'/api/articles/{draft_id}/draft')
        HTTP.checked(saved)
        actual, wanted = _ArticleHTML(saved.get('content')), _ArticleHTML(html)
        if (str(saved.get('id')) != draft_id or saved.get('title') != article.title
                or actual.text != wanted.text
                or [self._image_key(src) for src in actual.images] != [self._image_key(src) for src in wanted.images]):
            raise HTTPFailure('知乎草稿回读与原稿不一致，已停止发表')
        return draft_id

    @staticmethod
    def _image_key(url):
        if not isinstance(url, str):
            return ''
        try:
            parsed = urlsplit(url)
            host = parsed.hostname or ''
            if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443)
                    or not (host.endswith('.zhimg.com') or host == 'pic-private.zhihu.com')):
                return ''
        except ValueError:
            return ''
        path = parsed.path.lstrip('/')
        # Zhihu replaces private upload URLs with public resized variants when publishing.
        original = re.fullmatch(r'(v2-[a-f0-9]{32})(?:_[A-Za-z0-9]+)?(?:\.[A-Za-z0-9]+|~[^/]*)?', path)
        return original[1] if original else path

    def verify(self, article_id: str, *, expected: Article | None = None, evidence=None):
        if not article_id.isdigit():
            raise ValueError('知乎文章 ID 必须是数字')
        evidence = dict(evidence or {})
        if expected is not None:
            evidence.update(content_fingerprint('zhihu', expected.title, expected.body))
        data = self.http.json('GET', self.COLUMN + f'/api/articles/{article_id}')
        HTTP.checked(data)
        url = f'{self.COLUMN}/p/{article_id}'
        if str(data.get('id')) == article_id and data.get('state') == 'published':
            actual = _ArticleHTML(data.get('content'))
            media = evidence.get('media')
            if media and (not isinstance(media, list) or any(not isinstance(key, str) or not key for key in media)):
                raise HTTPFailure('知乎本地图片证据格式无效', kind='local_state_invalid')
            # Older checkpoints retained the full CDN path, including a rendition suffix.
            expected_media = [self._image_key('https://pic1.zhimg.com/' + key) for key in (media or [])]
            if ((evidence.get('sha256') and not content_matches('zhihu', data.get('title', ''), actual.text, evidence))
                    or (media and [self._image_key(src) for src in actual.images] != expected_media)):
                return PublishResult('pending', '知乎文章已存在，但正文、标题或配图未通过核对；不会重发', url, 'zhihu', 'mismatch')
            if not evidence.get('sha256') or not evidence.get('media'):
                return PublishResult('pending', '平台显示已发表；缺少原稿或上传图片证据，无法完整核验', url, 'zhihu', 'missing_evidence')
            return PublishResult('published', '知乎已发表，标题、完整正文和上传图片均通过回读核验', url, 'zhihu', 'verified')
        verification = 'mismatch' if str(data.get('id')) == article_id and data.get('state') == 'draft' else 'unavailable'
        return PublishResult('pending', f'知乎文章 {article_id} 尚无明确的已发表状态', url, 'zhihu', verification)

    def _publish_saved_draft(self, draft_id: str, article: Article, media: list[str]):
        if not draft_id.isdigit():
            raise ValueError('知乎草稿 ID 必须是数字')
        saved = self.http.json('GET', self.COLUMN + f'/api/articles/{draft_id}/draft')
        HTTP.checked(saved)
        parsed = _ArticleHTML(saved.get('content'))
        if (str(saved.get('id')) != draft_id or not media or any(not key for key in media)
                or not content_matches('zhihu', saved.get('title', ''), parsed.text,
                                       content_fingerprint('zhihu', article.title, article.body))
                or [self._image_key(src) for src in parsed.images] != media):
            raise HTTPFailure('发表前的草稿与原稿或上传图片不一致，已停止')
        options = {
            'commentPermission': 'anyone', 'disclaimer_type': 'none', 'disclaimer_status': 'close',
            'table_of_contents_enabled': False, 'content': saved['content'], 'title': article.title,
            'commercial_report_info': {'commercial_types': []}, 'commercial_zhitask_bind_info': None,
            'canReward': False,
        }
        body = json.dumps({'action': 'article', 'data': {
            'publish': {'traceId': f'{int(time.time() * 1000)},{uuid.uuid4()}'},
            'extra_info': {'publisher': 'pc', 'pc_business_params': json.dumps(options, ensure_ascii=False, separators=(',', ':'))},
            'draft': {'disabled': 1, 'id': draft_id, 'isPublished': False},
            'commentsPermission': {'comment_permission': 'anyone'},
            'creationStatement': {'disclaimer_type': 'none', 'disclaimer_status': 'close'},
            'contentsTables': {'table_of_contents_enabled': False},
            'commercialReportInfo': {'isReport': 0}, 'appreciate': {'can_reward': False, 'tagline': ''},
            'hybridInfo': {}, 'hybrid': {'html': saved['content'], 'textLength': len(parsed.text)},
            'title': {'title': article.title},
        }}, ensure_ascii=False, separators=(',', ':'))
        headers = zhihu_signing.publish_headers(self.http.session, body)
        data = self.http.json('POST', zhihu_signing.PUBLISH_URL, headers={**headers,
            'Content-Type': 'application/json', 'Referer': self.COLUMN + f'/p/{draft_id}/edit'}, data=body.encode('utf-8'))
        HTTP.checked(data)
        payload = data.get('data')
        if type(data.get('code')) is not int or data['code'] != 0 or not isinstance(payload, dict):
            raise HTTPFailure('知乎发表返回值无法确认成功，先核验原草稿，禁止重发')
        if isinstance(payload.get('write'), dict) and payload['write'].get('instruction') is True:
            raise HTTPFailure('知乎要求完成额外验证，已停止发表', kind='verification_required')
        try:
            result = json.loads(payload['result'])
            if not isinstance(result, dict) or str(result.get('publish', {}).get('id')) != draft_id:
                raise ValueError('mismatched article')
        except (KeyError, TypeError, ValueError, AttributeError):
            raise HTTPFailure('知乎发表回执未匹配当前文章，先核验，禁止重发', kind='invalid_response') from None

    def publish(self, article: Article, *, checkpoint=None):
        try:
            zhihu_signing.validate()
            self.account()
        except Exception as exc:
            return PublishResult('failed', f'知乎本地依赖或账号核验未通过：{type(exc).__name__}；尚未上传或创建文章', platform='zhihu')
        media = []
        def remember_media(key):
            if not key:
                raise HTTPFailure('知乎上传图片地址无效')
            media.append(key)
            if checkpoint:
                checkpoint('uploaded', key)
        try:
            draft_id = self.create_draft(article, checkpoint=(lambda value: checkpoint('draft', value)) if checkpoint else None,
                                         media_checkpoint=remember_media, _account_checked=True)
        except Exception as exc:
            return PublishResult('pending', f'知乎草稿阶段停止：{type(exc).__name__}；先核验草稿记录', platform='zhihu')
        try:
            self._publish_saved_draft(draft_id, article, media)
            if checkpoint:
                checkpoint('submitted', draft_id)
            return self.verify(draft_id, expected=article, evidence={'media': media})
        except Exception as exc:
            return PublishResult('pending', f'知乎文章 {draft_id} 结果待核验：{type(exc).__name__}；不会自动重发', platform='zhihu')
