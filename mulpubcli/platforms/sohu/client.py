"""搜狐号（sohu）平台适配器 — HTTP 图文投稿与回读。

登录凭证（cookie + dv-id + sp-cm + accountId）由浏览器登录组件换取并持久化到
`.storage/auth/sohu.json`（见 login.py / mulpubcli.browser）。本模块只做发布：
图片上传、存草稿/发布全部是纯 HTTP 请求，不依赖、不启动任何浏览器。

已实测（见 tmp/sohu-probe）：
* 图片上传  commons/front/outerUpload/image/file 纯 requests 可通（无 TLS 门禁）。
* 账号读取  mpbp/bp/account/list                 纯 HTTP 可通。
* 公开投稿  mpbp/bp/news/v4/news/publish/v2        需已授权 dv-id 与发布资格。
* 存草稿    mpbp/bp/news/v4/news/draft/v2          需已授权 dv-id。
"""
from __future__ import annotations

import io
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image

from mulpubcli.core import Article, PublishResult, content_fingerprint, content_matches
from mulpubcli.http import HTTP, HTTPFailure, iter_cookies, load_session, save_session
from mulpubcli.renderer import render as _render_article, _ArticleHTML, plain_markdown_text


PLATFORM = 'sohu'
MP = 'https://mp.sohu.com'
HOSTS = {'mp.sohu.com', 'res.mp.sohu.com'}
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36')
ACC_API = MP + '/mpbp/bp/account/list'
UPLOAD_API = MP + '/commons/front/outerUpload/image/file'
DRAFT_API = MP + '/mpbp/bp/news/v4/news/draft/v2'
PUBLISH_API = MP + '/mpbp/bp/news/v4/news/publish/v2'
LIST_API = MP + '/mpbp/bp/news/v4/users/news'
DETAIL_API = MP + '/mpbp/bp/news/v4/article'

# 搜狐号内容频道：图文=24（与浏览器实测一致）。
CHANNEL_ID = 24
CREATION_STATEMENTS = {'none': 0, 'fiction': 1, 'ai': 2,
                       'marketing': 3, 'reprint': 4, 'opinion': 5}


def _article_status(value) -> str:
    return {1: 'draft', 2: 'pending', 3: 'failed', 4: 'published',
            5: 'pending', 7: 'deleted', 9: 'deleted'}.get(value, 'pending')


def _cookie_header(session) -> str:
    """从会话组装 Cookie 头（含 .sohu.com 与 mp.sohu.com 两级域名）。"""
    pieces = []
    for cookie in iter_cookies(session):
        if cookie.value:
            pieces.append(f"{cookie.name}={cookie.value}")
    return '; '.join(pieces)


class SohuWeb:
    HOSTS = HOSTS

    def __init__(self, *, session=None, user_agent=None, network='direct',
                 account_id='', dv_id='', sp_cm=''):
        self.http = HTTP(self.HOSTS, session=session)
        if user_agent is not None:
            if not isinstance(user_agent, str) or any(c in user_agent for c in '\r\n'):
                raise ValueError('User-Agent 无效')
            self.http.session.headers['User-Agent'] = user_agent
        self.http.session.trust_env = network == 'environment'
        self.network = network
        self.account_id = account_id
        self.dv_id = dv_id
        self.sp_cm = sp_cm
        self.path = None
        self._auth_failed = False

    @classmethod
    def load(cls, path: Path):
        session, metadata = load_session(path, cls.HOSTS)
        for key in ('account_id', 'dv_id', 'sp_cm', 'user_agent', 'network'):
            if key in metadata and not isinstance(metadata[key], str):
                raise ValueError(f'搜狐凭证元数据 {key} 无效')
        client = cls(session=session,
                     user_agent=metadata.get('user_agent'),
                     network=metadata.get('network', 'direct'),
                     account_id=metadata.get('account_id', ''),
                     dv_id=metadata.get('dv_id', ''),
                     sp_cm=metadata.get('sp_cm', ''))
        client.path = path
        return client

    def save(self, path: Path):
        save_session(path, self.http.session, user_agent=self.http.session.headers.get('User-Agent', USER_AGENT),
                     network=self.network, account_id=self.account_id,
                     dv_id=self.dv_id, sp_cm=self.sp_cm)
        self.path = path

    def _persist(self):
        if self.path is not None:
            self.save(self.path)

    def close(self):
        try:
            self.http.session.close()
        except Exception:
            pass

    def _write_headers(self) -> dict:
        """搜狐写操作（上传/存稿）需要的自定义头。"""
        return {
            'Origin': MP, 'Referer': MP + '/',
            'X-Requested-With': 'XMLHttpRequest',
            'dv-id': self.dv_id, 'sp-cm': self.sp_cm,
        }

    def account(self) -> dict:
        """核验搜狐登录态并返回账号信息。"""
        r = self.http.request('GET', ACC_API + '?_=' + str(int(time.time() * 1000)),
                              headers={'X-Requested-With': 'XMLHttpRequest', **self._write_headers()})
        try:
            payload = r.json()
        except ValueError:
            raise HTTPFailure('搜狐返回无效 JSON，已停止', kind='invalid_response') from None
        if payload.get('code') != 2000000:
            raise HTTPFailure('搜狐登录态无效，需重新 mulpubcli login sohu', kind='authentication_required')
        data = payload.get('data') or {}
        groups = data.get('data') or []
        accounts = [acc for group in groups for acc in (group.get('accounts') or [])
                    if acc.get('id') is not None]
        if not self.account_id:
            if len(accounts) != 1:
                kind = 'authentication_required' if not accounts else 'account_selection_required'
                raise HTTPFailure('搜狐未找到唯一可用账号，无法完成登录核验', kind=kind)
            self.account_id = str(accounts[0]['id'])
        for acc in accounts:
            if str(acc['id']) == str(self.account_id):
                details = {}
                for source, target in (('accountTypeName', 'account_type'),
                                       ('statusName', 'account_status'),
                                       ('homePage', 'homepage')):
                    value = acc.get(source)
                    if isinstance(value, str) and value:
                        details[target] = value
                return {'id': str(acc['id']), 'name': acc.get('nickName', ''),
                        'account_details': details}
        raise HTTPFailure('搜狐未找到保存的账号', kind='account_mismatch')

    # ─────────────────────────────────────────────
    # 图片上传（纯 HTTP）
    # ─────────────────────────────────────────────

    def upload_image(self, path: Path) -> str:
        """上传本地图片到搜狐 CDN，返回图片 URL（res.mp.sohu.com）。"""
        if not self.account_id:
            raise HTTPFailure('搜狐尚无登录凭证；请先登录', kind='authentication_required')
        with Image.open(path) as image:
            mime = Image.MIME.get(image.format, 'image/jpeg')
            image.verify()
        data = path.read_bytes()
        files = {
            'file': (path.name, io.BytesIO(data), mime),
            'accountId': (None, self.account_id),
        }
        r = self.http.request('POST', UPLOAD_API + f'?accountId={self.account_id}',
                              files=files, headers=self._write_headers())
        try:
            payload = r.json()
        except ValueError:
            raise HTTPFailure('搜狐上传返回无效 JSON，已停止', kind='invalid_response') from None
        url = payload.get('url', '')
        if not isinstance(url, str) or not url:
            raise HTTPFailure('搜狐图片上传失败', kind='invalid_response')
        return url

    # ─────────────────────────────────────────────
    # 存草稿 / 发布（纯 HTTP）
    # ─────────────────────────────────────────────

    def _save_draft(self, title: str, content_html: str, cover: str,
                    account_id: int, brief: str = '') -> str:
        body = {
            'title': title, 'brief': brief, 'content': content_html,
            'channelId': CHANNEL_ID, 'categoryId': -1, 'id': 0, 'userColumnId': 0,
            'columnNewsIds': [], 'businessCode': 0, 'declareOriginal': False,
            'cover': cover, 'topicIds': [], 'isAd': 0, 'userLabels': '[]',
            'reprint': False, 'customTags': '', 'infoResource': 0, 'sourceUrl': '',
            'visibleToLoginedUsers': 0, 'attrIds': [], 'auto': True,
            'accountId': account_id,
        }
        r = self.http.request('POST', DRAFT_API + f'?accountId={account_id}',
                              json=body, headers={'Content-Type': 'application/json', **self._write_headers()})
        try:
            payload = r.json()
        except ValueError:
            raise HTTPFailure('搜狐存稿返回无效 JSON，已停止', kind='invalid_response') from None
        if payload.get('code') == 1300:
            raise HTTPFailure('搜狐客户端未授权，需重新 mulpubcli login sohu', kind='verification_required')
        if not payload.get('success'):
            raise HTTPFailure(f"搜狐存稿失败：{payload.get('msg')}", kind='platform_rejected')
        post_id = payload.get('data')
        if not isinstance(post_id, (str, int)) or not str(post_id):
            raise HTTPFailure('搜狐未返回草稿 ID，结果待核验', kind='invalid_response')
        return str(post_id)

    def publish(self, article: Article, *, checkpoint=None, declaration: str = 'none') -> PublishResult:
        """直接提交图文到公开发布接口，审核结果交由 verify 回读。"""
        if declaration not in CREATION_STATEMENTS:
            raise ValueError('搜狐创作声明无效')
        try:
            self.account()
        except HTTPFailure as exc:
            return PublishResult('failed', f'搜狐登录态核验未通过：{exc}；尚未上传', platform=PLATFORM)

        submitted = False
        try:
            cover_url = self.upload_image(article.cover)
            if checkpoint:
                checkpoint('uploaded', cover_url)
            image_map = {str(article.cover): cover_url}
            for img_path in article.body_images:
                image_url = self.upload_image(img_path)
                image_map[str(img_path)] = image_url
                if checkpoint:
                    checkpoint('uploaded', image_url)
            html = _render_article(article, image_map, cover_first=False, include_title=False)
            body = {
                'title': article.title, 'brief': article.summary or '', 'content': html,
                'channelId': CHANNEL_ID, 'categoryId': -1, 'id': 0,
                'userColumnId': 0, 'columnNewsIds': [], 'businessCode': 0,
                'declareOriginal': False, 'cover': cover_url, 'topicIds': [],
                'isAd': 0, 'userLabels': '[]', 'reprint': False, 'customTags': '',
                'creationStatement': CREATION_STATEMENTS[declaration], 'additionalStatement': None,
                'sourceUrl': '', 'visibleToLoginedUsers': 0, 'attrIds': [],
                'accountId': int(self.account_id),
            }
            submitted = True
            response = self.http.request(
                'POST', PUBLISH_API + f'?accountId={self.account_id}', json=body,
                headers={'Content-Type': 'application/json', **self._write_headers()})
            try:
                payload = response.json()
            except ValueError:
                return PublishResult('pending', '搜狐投稿回执无效；请用 verify 核验，勿重发',
                                     platform=PLATFORM, verification='unavailable')
            if not isinstance(payload, dict):
                return PublishResult('pending', '搜狐投稿回执格式未知；请用 verify 核验，勿重发',
                                     platform=PLATFORM, verification='unavailable')
            if payload.get('code') != 2000000:
                return PublishResult('failed', f"搜狐公开投稿被拒绝：{payload.get('msg') or '平台未给出原因'}",
                                     platform=PLATFORM, verification='rejected')
            data = payload.get('data')
            remote_id = (data.get('id') if isinstance(data, dict) else data)
            if not isinstance(remote_id, (str, int)) or not str(remote_id).isdigit():
                return PublishResult('pending', '搜狐已受理投稿，但未返回可确认的文章 ID；请用 list 核验',
                                     platform=PLATFORM, verification='unavailable')
            remote_id = str(remote_id)
            if isinstance(data, dict):
                if checkpoint:
                    checkpoint('submitted', remote_id)
                return PublishResult('pending', f'搜狐文章 {remote_id} 需要完成平台的后续确认；勿重发',
                                     platform=PLATFORM, verification='unavailable')
            if checkpoint:
                checkpoint('submitted', remote_id)
            return PublishResult('pending', f'搜狐文章 {remote_id} 已提交，等待平台审核与回读',
                                 platform=PLATFORM, verification='unavailable')
        except HTTPFailure as exc:
            status = 'pending' if submitted else 'failed'
            return PublishResult(status, f'搜狐投稿过程停止：{exc}' + ('；请回读核验，勿重发' if submitted else ''),
                                 platform=PLATFORM, verification='unavailable' if submitted else None)

    def list_articles(self) -> list[dict]:
        """读取搜狐图文列表，保留审核中与草稿等平台状态。"""
        self.account()
        self._last_list_complete = False
        found = []
        page_size = 50
        for page in range(1, 51):
            response = self.http.request('GET', LIST_API, params={
                'psize': page_size, 'newsType': 1, 'statusType': 1,
                'pno': page, 'accountId': self.account_id}, headers=self._write_headers())
            try:
                payload = response.json()
            except ValueError:
                raise HTTPFailure('搜狐文章列表返回无效 JSON', kind='invalid_response') from None
            if not isinstance(payload, dict) or payload.get('code') != 2000000:
                raise HTTPFailure('搜狐文章列表读取失败', kind='invalid_response')
            data = payload.get('data') or {}
            rows = data.get('news')
            if not isinstance(rows, list):
                raise HTTPFailure('搜狐文章列表缺少 news', kind='invalid_response')
            for row in rows:
                if not isinstance(row, dict) or not str(row.get('id') or '').isdigit():
                    continue
                status = _article_status(row.get('status'))
                remote_id = str(row['id'])
                user_id = str(row.get('userId') or self.account_id)
                url = (f'https://www.sohu.com/a/{remote_id}_{user_id}'
                       if status == 'published' and user_id.isdigit() else None)
                found.append({'id': remote_id, 'title': str(row.get('title') or ''),
                              'status': status, 'published_at': row.get('postTime'), 'url': url,
                              'check': (f'搜狐列表显示文章已下架或删除（状态码 {row.get("status")}）'
                                        if status == 'deleted' else None)})
            total = data.get('totalCount')
            if len(rows) < page_size or (isinstance(total, int) and page * page_size >= total):
                self._last_list_complete = True
                break
        return found

    def article_detail(self, article_id: str) -> dict | None:
        """Read one known article ID even when status filters omit it from the feed."""
        if not article_id.isdigit():
            raise ValueError('搜狐文章 ID 必须是数字')
        response = self.http.request('GET', DETAIL_API, params={
            'newsId': article_id, 'accountId': self.account_id}, headers=self._write_headers())
        try:
            payload = response.json()
        except ValueError:
            raise HTTPFailure('搜狐文章详情返回无效 JSON', kind='invalid_response') from None
        if not isinstance(payload, dict) or payload.get('code') != 2000000:
            raise HTTPFailure('搜狐文章详情读取失败，保留本地状态', kind='invalid_response')
        data = payload.get('data')
        detail = data.get('news') if isinstance(data, dict) else None
        if not isinstance(detail, dict):
            return None
        if str(detail.get('id')) != article_id:
            raise HTTPFailure('搜狐文章详情 ID 与请求不一致', kind='invalid_response')
        user_id = str(detail.get('userId') or '')
        if self.account_id and user_id and user_id != str(self.account_id):
            raise HTTPFailure('搜狐文章详情所属账号与当前账号不同', kind='account_mismatch')
        status = _article_status(detail.get('status'))
        url = (f'https://www.sohu.com/a/{article_id}_{user_id}'
               if status == 'published' and user_id.isdigit() else None)
        check = (f"搜狐详情显示文章已下架或删除（状态码 {detail.get('status')}）；公开链接不可访问"
                 if status == 'deleted' else None)
        return {'id': article_id, 'title': str(detail.get('title') or ''),
                'status': status, 'published_at': detail.get('postTime') or detail.get('createdTime'),
                'url': url, 'check': check}

    def verify(self, article_id: str, *, expected: Article | None = None, evidence=None) -> PublishResult:
        """Use the official content list to distinguish review from publication."""
        if not article_id.isdigit():
            raise ValueError('搜狐文章 ID 必须是数字')
        row = next((item for item in self.list_articles() if item['id'] == article_id), None)
        if row is None:
            row = self.article_detail(article_id)
            if row is None:
                return PublishResult('pending', '搜狐列表和单篇详情均未找到该文章，不能判定删除',
                                     platform=PLATFORM, verification='unavailable')
        status = row['status']
        if status == 'deleted':
            return PublishResult('deleted', row.get('check') or '搜狐详情显示文章已下架或删除；公开链接不可访问',
                                 platform=PLATFORM, verification='verified')
        if status == 'draft':
            return PublishResult('draft', '搜狐草稿仍在，尚未公开发表',
                                 platform=PLATFORM, verification='published')
        if status == 'failed':
            return PublishResult('failed', '搜狐审核未通过', platform=PLATFORM, verification='rejected')
        if status != 'published':
            return PublishResult('pending', '搜狐文章仍在审核或定时发布中',
                                 platform=PLATFORM, verification='unavailable')
        url = row['url']
        if expected is not None and row['title'] != expected.title:
            return PublishResult('pending', '搜狐列表标题与原稿不一致', url, PLATFORM, 'mismatch')
        proof = dict(evidence or {})
        if expected is not None:
            proof.update(content_fingerprint(PLATFORM, expected.title, plain_markdown_text(expected.body)))
        if proof.get('sha256'):
            response = self.http.request('GET', DETAIL_API, params={'newsId': article_id,
                                                                     'accountId': self.account_id},
                                         headers=self._write_headers())
            try:
                payload = response.json()
            except ValueError:
                raise HTTPFailure('搜狐文章详情返回无效 JSON', kind='invalid_response') from None
            data = payload.get('data') if isinstance(payload, dict) else None
            detail = data.get('news') if isinstance(data, dict) else None
            if not isinstance(payload, dict) or payload.get('code') != 2000000 or not isinstance(detail, dict):
                return PublishResult('published', '搜狐已发布；文章详情暂不可用于内容核验',
                                     url, PLATFORM, 'published')
            parsed = _ArticleHTML(detail.get('content') or '')
            if not content_matches(PLATFORM, detail.get('title') or '', parsed.text, proof):
                return PublishResult('pending', '搜狐文章已发布，但正文与原稿不一致',
                                     url, PLATFORM, 'mismatch')
            media = proof.get('media')
            if isinstance(media, list) and media:
                def image_key(value):
                    parsed_url = urlsplit(value)
                    return (parsed_url.hostname, parsed_url.path)
                if any(not isinstance(value, str) for value in media):
                    return PublishResult('pending', '搜狐文章已发布，但配图证据格式无效',
                                         url, PLATFORM, 'mismatch')
                actual_images = [image_key(src) for src in parsed.images]
                expected_images = [image_key(src) for src in media]
                if detail.get('cover'):
                    images_match = (image_key(detail['cover']) == expected_images[0]
                                    and actual_images == expected_images[1:])
                else:
                    images_match = actual_images == expected_images
                if not images_match:
                    expected_count = len(expected_images) - (1 if detail.get('cover') else 0)
                    if len(actual_images) == expected_count and all(host and path for host, path in actual_images):
                        return PublishResult('published',
                                             '搜狐已发布，标题正文与图片数量一致；平台重写图片地址，未完成图片内容及顺序核对',
                                             url, PLATFORM, 'published')
                    return PublishResult('pending', '搜狐文章已发布，但配图与原稿证据不一致',
                                         url, PLATFORM, 'mismatch')
                return PublishResult('published', '搜狐已发布，标题、正文与配图回读一致',
                                     url, PLATFORM, 'verified')
            return PublishResult('published', '搜狐已发布，标题与正文一致；缺少配图证据',
                                 url, PLATFORM, 'published')
        return PublishResult('published', '搜狐已发布；缺少原稿证据，未做完整内容比对',
                             url, PLATFORM, 'published')

    def draft(self, article: Article, *, checkpoint=None) -> PublishResult:
        """把图文和图片保存为搜狐草稿。"""
        try:
            self.account()
        except HTTPFailure as exc:
            return PublishResult('failed', f'搜狐登录态核验未通过：{exc}；尚未上传', platform=PLATFORM)

        try:
            cover_url = self.upload_image(article.cover)
            if checkpoint:
                checkpoint('uploaded', 'cover')
            image_map: dict[str, str] = {str(article.cover): cover_url}
            for img_path in article.body_images:
                url = self.upload_image(img_path)
                image_map[str(img_path)] = url
                if checkpoint:
                    checkpoint('uploaded', str(img_path))
            html = _render_article(article, image_map, cover_first=False, include_title=False)
            post_id = self._save_draft(article.title, html, cover_url, int(self.account_id),
                                       brief=article.summary or '')
            if checkpoint:
                checkpoint('draft', post_id)
            draft_url = (f'{MP}/mpfe/v4/contentManagement/news/addarticle'
                         f'?spm=smmp.articlelist.0.0&contentStatus=2&id={post_id}')
            return PublishResult('draft', '搜狐号已保存草稿，图片与封面已一并上传',
                                  url=draft_url, platform=PLATFORM, verification='unavailable')
        except HTTPFailure as exc:
            return PublishResult('failed', f'搜狐发布失败：{exc}', platform=PLATFORM)
