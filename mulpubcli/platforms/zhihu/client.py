"""Cookie-based Zhihu article workflow; no replay of expired signing headers."""
from __future__ import annotations

import hashlib
import json
import re
import time
import os
import uuid
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image

from mulpubcli.core import Article, PublishResult, content_fingerprint, content_matches, strip_markdown_images
from mulpubcli.http import HTTP, HTTPFailure, chrome_session, iter_cookies, load_session, save_session
from mulpubcli.renderer import render as _render_article, _ArticleHTML
from . import cookies as zhihu_cookies
from . import signing as zhihu_signing


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
        # 默认使用 curl_cffi 伪装真实 Chrome 的 TLS 指纹。裸 requests 的 TLS 特征
        # 一眼可辨，会被知乎 WAF 判为非浏览器并引导到 /account/unhuman；浏览器登录
        # 成功正是因为它是真实 Chrome 指纹。可注入自定义 session 以保持兼容。
        self.http = HTTP(self.HOSTS, session=session if session is not None else chrome_session())
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

    @classmethod
    def import_cookies(cls, source: Path, destination: Path):
        """从浏览器导出的 Cookie 文件导入知乎登录态并原子保存凭证。

        解析浏览器导出的 Cookie 文件（原始 Cookie 头 / Chrome 扩展 JSON /
        Playwright JSON / Netscape cookies.txt），只保留知乎域名与未过期的
        z_c0，调用 account() 核验账号登录态，核验成功才写入正式凭证；核验失败
        不会覆盖已有的有效凭证。导入不保留旧二维码登录状态。
        """
        return cls.import_cookie_text(
            source.read_text(encoding='utf-8', errors='replace'), destination)

    @classmethod
    def import_cookie_text(cls, text: str, destination: Path):
        """从 Cookie 文本（而非文件）导入知乎登录态并原子保存凭证。

        供终端无回显粘贴（--cookie-stdin）使用；解析、校验与核验逻辑与
        import_cookies 完全一致。该路径不把 Cookie 明文落盘。
        """
        records = zhihu_cookies.parse_cookie_text(text)
        records = zhihu_cookies.zhihu_records(records)
        zhihu_cookies.require_login_cookie(records)
        session = zhihu_cookies.build_session(records)
        client = cls(session=session, network='direct')
        try:
            info = client.account()
            # 只写 Cookie 与账号元数据，不残留任何旧二维码登录字段。
            client.qr_token, client.qr_link = '', ''
            client.qr_expires_at, client.login_blocked = 0, False
            client.save(destination)
            return info
        finally:
            client.close()

    def account(self):
        if not any(cookie.name == 'z_c0' and cookie.value and not cookie.is_expired()
                   for cookie in iter_cookies(self.http.session)):
            raise HTTPFailure('知乎尚无有效登录 Cookie；请导入浏览器导出的知乎 Cookie 文件', kind='authentication_required')
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
            if media_checkpoint:
                media_checkpoint(self._image_key(body_img_url))
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
            # 渲染后正文只保留可见文本与独立配图，markdown 图片语法不进入内容，
            # 指纹须基于剥离图片语法后的正文计算。
            evidence.update(content_fingerprint('zhihu', expected.title, strip_markdown_images(expected.body)))
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

    def article_state(self, article_id: str) -> str:
        """实时单篇状态，供本地台账逐条回查：'published' | 'draft' | 'not_found' | 'unknown'。

        知乎没有"我的已发布文章"列表接口，只能按 ID 查单篇。404 视为已删除，
        由调用方把它从发布列表移出。
        """
        if not article_id.isdigit():
            raise ValueError('知乎文章 ID 必须是数字')
        try:
            data = self.http.json('GET', self.COLUMN + f'/api/articles/{article_id}')
        except HTTPFailure as exc:
            if exc.status_code == 404:
                return 'not_found'
            raise
        if isinstance(data, dict) and str(data.get('id')) == article_id:
            if data.get('state') == 'published':
                return 'published'
            if data.get('state') == 'draft':
                return 'draft'
        return 'unknown'

    def _publish_saved_draft(self, draft_id: str, article: Article, media: list[str]):
        if not draft_id.isdigit():
            raise ValueError('知乎草稿 ID 必须是数字')
        saved = self.http.json('GET', self.COLUMN + f'/api/articles/{draft_id}/draft')
        HTTP.checked(saved)
        parsed = _ArticleHTML(saved.get('content'))
        if (str(saved.get('id')) != draft_id or not media or any(not key for key in media)
                or not content_matches('zhihu', saved.get('title', ''), parsed.text,
                                       content_fingerprint('zhihu', article.title, strip_markdown_images(article.body)))
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
