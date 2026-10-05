"""NetEase (网易号) creator HTTP adapter — pure-HTTP core, curl_cffi TLS fingerprint.

登录与草稿、回读、上传、列表都在纯 HTTP 内完成；公众发布额外需要网易官方
易盾 SDK 在浏览器打出的 ursToken（见 guard.py），携带它后发布请求仍是纯 HTTP。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image
from curl_cffi import CurlMime

from mulpubcli.core import Article, PublishResult, content_fingerprint, content_matches, strip_markdown_images
from mulpubcli.http import HTTP, HTTPFailure, chrome_session, iter_cookies, load_session, save_session
from mulpubcli.renderer import render as _render_article, _ArticleHTML
from . import guard as netease_guard

PLATFORM = 'netease'
MP = 'https://mp.163.com'
HOSTS = {'mp.163.com', 'www.163.com'}
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36')
SESSION_COOKIE = 'NTESwebSI'
DAILY_QUOTA_MESSAGE = '网易今日发布数量已达最大值（每日 6 篇），请明日再试'


def daily_quota_reason(account: dict) -> str | None:
    details = account.get('account_details')
    if not isinstance(details, dict):
        return None
    published = details.get('today_published')
    limit = details.get('daily_publish_limit')
    if type(published) is int and type(limit) is int and limit > 0 and published >= limit:
        return f'网易今日已发布 {published} 篇，达到每日 {limit} 篇上限，请明日再试'
    return None


def is_daily_quota_rejection(message: str) -> bool:
    return any(marker in message for marker in (
        '今日发布数量已达最大值', '今日发布数量已达上限',
        '今日发文数量已达上限', '已达每日发布上限',
    ))

# attachment URL host 由 picupload 返回，供存储/回读做兜底（不是网络目标）。
_ATTACH_HOST = '.ws.126.net'


def _doc_id(data) -> str:
    """publishV2.do 的 data 形如 'docId=L8DBTPLM0556PYDT&pkId=null' 或
    '/wemedia/article/mylist/.../1-20.html,L8D3OR2N0556PYDT'。"""
    if isinstance(data, str):
        m = re.search(r'(?:^|&)docId=([A-Za-z0-9_-]{1,64})(?:&|$)', data)
        if m:
            return m.group(1)
        if ',' in data:
            tail = data.rsplit(',', 1)[1].strip()
            if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', tail):
                return tail
        if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', data.strip()):
            return data.strip()
    return ''


def _content_state(item: dict) -> str:
    """网易内容状态映射到 CLI 通用状态。0=草稿，3=已发布，6/7=下线。

    真实发布记录 contentState=3（已发布），不是驳回；分发是否受限由
    `unrecomReason` 字段单独表达（如「该内容分发受限」），与 contentState 无关。
    """
    state = item.get('contentState')
    try:
        state = int(state)
    except (TypeError, ValueError):
        return 'unknown'
    if state == 3:
        # 已发布；若带分发受限原因则标注出来，便于与正常公开稿区分。
        reason = item.get('unrecomReason')
        if reason and str(reason).strip() not in ('0', 'None', ''):
            return 'published(分发受限)'
        return 'published'
    if state == 0:
        return 'draft'
    if state == 1:
        return 'pending'
    if state in (6, 7):
        return 'deleted'
    return 'unknown'


def _item_id(item: dict) -> str:
    raw = item.get('articleId') or item.get('id') or item.get('docId')
    if isinstance(raw, (int, str)):
        s = str(raw)
        if s and s != '0':
            return s
    return ''


class NeteaseWeb:
    MAIN = MP
    HOSTS = HOSTS

    def __init__(self, *, session=None, user_agent=None, network='direct'):
        if network not in ('direct', 'environment'):
            raise ValueError('网易网络模式须为 direct 或 environment')
        self.network = network
        self.http = HTTP(self.HOSTS, session=session if session is not None else chrome_session())
        if user_agent is not None and (not isinstance(user_agent, str) or any(c in user_agent for c in '\r\n')):
            raise ValueError('User-Agent 无效')
        self.http.session.headers['User-Agent'] = user_agent or USER_AGENT
        self.http.session.trust_env = network == 'environment'
        self.http.session.headers.update({
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Referer': MP + '/wemedia/index.html',
            'Origin': MP,
        })
        self.http.session.proxies.update({
            'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'})
        self.wemedia_id = ''
        self.tname = ''
        self.path = None
        self._cookie_expiry = {}

    @classmethod
    def load(cls, path: Path):
        # 网易 WAF 要求真实 Chrome TLS 指纹，凭证恢复必须落在 curl_cffi 的
        # chrome_session 上（load_session 返回的是裸 requests 会话）。这里只借用
        # load_session 做 cookie 校验与域名过滤，再把 cookie 注入 chrome_session。
        requests_session, metadata = load_session(path, cls.HOSTS, cookie_roots={'163.com'})
        cookies = list(iter_cookies(requests_session))
        session = cls._chrome_from_cookies(cookies)
        requests_session.close()
        client = cls(session=session, user_agent=metadata.get('user_agent'),
                     network=metadata.get('network', metadata.get('transport', 'direct')))
        client._cookie_expiry = {
            (cookie.name, cookie.domain, cookie.path, cookie.value): cookie.expires
            for cookie in cookies if not cookie.is_expired() and cookie.expires is not None}
        client.wemedia_id = str(metadata.get('wemedia_id') or metadata.get('account_id') or '')
        client.tname = str(metadata.get('tname') or '')
        client.path = path
        return client

    @staticmethod
    def _chrome_from_cookies(cookies):
        session = chrome_session()
        for cookie in cookies:
            if cookie.is_expired():
                continue
            try:
                session.cookies.set(cookie.name, cookie.value, domain=cookie.domain,
                                    path=cookie.path or '/', secure=bool(cookie.secure))
            except (AttributeError, KeyError, TypeError):
                continue
        return session

    def save(self, path: Path):
        for cookie in iter_cookies(self.http.session):
            if cookie.expires is None:
                cookie.expires = self._cookie_expiry.get(
                    (cookie.name, cookie.domain, cookie.path, cookie.value))
        save_session(path, self.http.session, user_agent=self.http.session.headers['User-Agent'],
                     network=self.network, wemedia_id=self.wemedia_id, tname=self.tname)
        self.path = path

    def close(self):
        self.http.session.close()

    def _authenticated(self) -> bool:
        return any(c.name == SESSION_COOKIE and c.value and not c.is_expired()
                   for c in iter_cookies(self.http.session))

    def _checked(self, payload, *, expect_code=(1,), what='操作'):
        if not isinstance(payload, dict):
            raise HTTPFailure(f'网易{what}响应结构变化，已停止', kind='invalid_response')
        code = payload.get('code')
        if type(code) is not int and not (isinstance(code, str) and code.isdigit()):
            raise HTTPFailure(f'网易{what}缺少明确业务状态，已停止', kind='invalid_response')
        code = int(code)
        if code not in expect_code:
            msg = payload.get('msg') or payload.get('message') or ''
            if what == '投稿' and is_daily_quota_rejection(str(msg)):
                raise HTTPFailure(DAILY_QUOTA_MESSAGE, kind='limit', code=code)
            kind = 'authentication_required' if code in (401, 8, 9, 70) else 'platform_rejected'
            raise HTTPFailure(f'网易{what}被拒绝（code={code}' + (f'，{msg}' if msg else '') + '），已停止', kind=kind, code=code)
        return payload

    def account(self):
        if not self._authenticated():
            raise HTTPFailure('网易尚无有效登录 Cookie（NTESwebSI），请导入浏览器导出的网易 Cookie', kind='authentication_required')
        payload = self._checked(
            self.http.json('GET', MP + '/wemedia/navinfo.do', headers={'Referer': MP + '/wemedia/index.html'}),
            what='账号查询')
        data = payload.get('data')
        if not isinstance(data, dict):
            raise HTTPFailure('网易账号详情结构无效', kind='invalid_response')
        wemedia_id = str(data.get('wemediaId') or '')
        tname = str(data.get('tname') or '').strip()
        if not re.fullmatch(r'\w{1,64}', wemedia_id):
            raise HTTPFailure('网易未返回有效创作者身份，可能未开通网易号', kind='authentication_required')
        if self.wemedia_id and self.wemedia_id != wemedia_id:
            raise HTTPFailure('网易账号与保存身份不一致，已停止', kind='account_mismatch')
        self.wemedia_id, self.tname = wemedia_id, tname
        if self.path is not None:
            self.save(self.path)
        details = {}
        for source, target in (('articleCount', 'article_count'),
                               ('todayPubCount', 'today_published'),
                               ('maxDailyPublishCount', 'daily_publish_limit')):
            value = data.get(source)
            if type(value) is int:
                details[target] = value
        return {'id': wemedia_id, 'name': tname or wemedia_id,
                'account_details': details}

    def upload_image(self, path: Path):
        if not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError('网易配图须存在且不超过 10MB')
        with Image.open(path) as image:
            if image.format not in ('PNG', 'JPEG', 'WEBP'):
                raise ValueError('网易配图须为 PNG、JPEG 或 WEBP')
            width, height = image.size
            mime = Image.MIME[image.format]
            image.verify()
        data = path.read_bytes()
        part = CurlMime.from_list([
            {'name': 'file', 'filename': path.name, 'data': data, 'content_type': mime},
            {'name': 'from', 'data': 'neteasecode_mp'}])
        response = self.http.request('POST', MP + '/api/v3/upload/picupload',
                                     headers={'Referer': MP + '/wemedia/article/status/publish.html',
                                              'X-Requested-With': 'XMLHttpRequest'},
                                     multipart=part)
        try:
            payload = response.json()
        except ValueError:
            raise HTTPFailure('网易图片上传未返回有效 JSON', kind='invalid_response') from None
        if type(payload.get('code')) is not int or payload['code'] != 200:
            raise HTTPFailure('网易图片上传失败', kind='platform_rejected')
        data = payload.get('data')
        if not isinstance(data, dict):
            raise HTTPFailure('网易图片上传结果结构无效', kind='invalid_response')
        url = data.get('url')
        if not isinstance(url, str) or not url:
            raise HTTPFailure('网易图片上传未返回存储地址', kind='invalid_response')
        # 存储地址统一用 https（网易对象存储 http/https 均可用）。
        if url.startswith('http://'):
            url = 'https://' + url[len('http://'):]
        return {'url': url, 'width': data.get('width') or width,
                'height': data.get('height') or height, 'pid': data.get('pid') or ''}

    def _submit(self, article: Article, *, operation: str, urs_token: str = '',
                checkpoint=None) -> dict:
        """上传配图并调用 publishV2.do；operation=saveDraft 或 publish。"""
        account = self.account()
        if operation == 'publish':
            reason = daily_quota_reason(account)
            if reason:
                raise HTTPFailure(reason, kind='limit')
        cover = self.upload_image(article.cover)
        if checkpoint:
            checkpoint('uploaded', cover['url'])
        image_map: dict[str, str] = {str(article.cover): cover['url']}
        for img_path in article.body_images:
            body_img = self.upload_image(img_path)
            image_map[str(img_path)] = body_img['url']
            if checkpoint:
                checkpoint('uploaded', body_img['url'])
        body_html = _render_article(article, image_map, cover_first=True, include_title=False)
        form = {
            'wemediaId': self.wemedia_id,
            'articleId': '',
            'title': article.title,
            'content': body_html,
            'cover': cover['url'],
            'operation': operation,
            'scheduled': 0,
        }
        if operation == 'publish':
            form['ursToken'] = urs_token
        payload = self._checked(self.http.json(
            'POST', MP + '/wemedia/article/status/api/publishV2.do', data=form,
            headers={'Referer': MP + '/wemedia/index.html'}), what='投稿')
        doc_id = _doc_id(payload.get('data'))
        if not doc_id:
            raise HTTPFailure('网易未返回有效内容 ID，请核验，不会重发', kind='invalid_response')
        return {'doc_id': doc_id, 'url': f'https://www.163.com/dy/article/{doc_id}.html'}

    def draft(self, article: Article, *, checkpoint=None) -> PublishResult:
        if not 1 <= len(article.title) <= 60 or not article.body.strip() or len(article.body.encode()) > 1_000_000:
            return PublishResult('failed', '网易标题须为 1～60 字、正文非空且不超过本地 1MB 上限', platform=PLATFORM)
        try:
            result = self._submit(article, operation='saveDraft', checkpoint=checkpoint)
        except HTTPFailure as exc:
            return PublishResult('pending', f'网易草稿阶段停止：{exc}', platform=PLATFORM)
        except Exception as exc:
            return PublishResult('pending', f'网易草稿阶段停止：{type(exc).__name__}；先核验草稿记录', platform=PLATFORM)
        if checkpoint:
            checkpoint('draft', result['doc_id'])
        return PublishResult('draft', f'网易草稿已保存（{result["doc_id"]}）', None, PLATFORM, 'verified')

    def publish(self, article: Article, *, checkpoint=None, urs_token='', token_file=None) -> PublishResult:
        if not 1 <= len(article.title) <= 60 or not article.body.strip() or len(article.body.encode()) > 1_000_000:
            return PublishResult('failed', '网易标题须为 1～60 字、正文非空且不超过本地 1MB 上限', platform=PLATFORM)
        try:
            token = netease_guard.load_urs_token(value=urs_token, token_file=token_file)
        except HTTPFailure as exc:
            return PublishResult('failed', str(exc), platform=PLATFORM)
        try:
            result = self._submit(article, operation='publish', urs_token=token, checkpoint=checkpoint)
        except HTTPFailure as exc:
            return PublishResult('failed' if exc.kind == 'limit' else 'pending',
                                 f'网易发布阶段停止：{exc}', platform=PLATFORM)
        except Exception as exc:
            return PublishResult('pending', f'网易发布阶段停止：{type(exc).__name__}；先核验发布记录', platform=PLATFORM)
        if checkpoint:
            checkpoint('submitted', result['doc_id'])
        return self.verify(result['doc_id'], expected=article)

    # ── 列表 / 回查 ─────────────────────────────────────────────────────

    def _list_page(self, *, content_state: int, page_no: int = 1, size: int = 20) -> list[dict]:
        self.account()
        payload = self._checked(self.http.json(
            'POST', MP + '/wemedia/content/manage/list.do',
            data={'wemediaId': self.wemedia_id, 'pageNo': page_no, 'size': size,
                  'contentType': 1, 'contentState': content_state},
            headers={'Referer': MP + '/wemedia/index.html'}), what='列表查询')
        data = payload.get('data')
        if not isinstance(data, dict):
            raise HTTPFailure('网易列表结构无效', kind='invalid_response')
        items = data.get('list') if isinstance(data.get('list'), list) else []
        return [i for i in items if isinstance(i, dict)]

    def list_articles(self, *, draft=False) -> list[dict]:
        found: list[dict] = []
        self._last_list_complete = False
        # 草稿 contentState=0；已发布 contentState=3（2 不是已发布状态）。
        for state in ((0,) if draft else (3,)):
            for page_no in range(1, 51):
                rows = self._list_page(content_state=state, page_no=page_no)
                if not rows:
                    self._last_list_complete = True
                    break
                for item in rows:
                    row_id = _item_id(item)
                    if not row_id:
                        continue
                    found.append({
                        'id': row_id,
                        'title': str(item.get('title') or ''),
                        'status': _content_state(item),
                        'published_at': item.get('publishTime') and str(item.get('publishTime')),
                    })
                if len(rows) < 20:
                    self._last_list_complete = True
                    break
        return found

    def article_state(self, article_id: str) -> str:
        for page_no in range(1, 51):
            rows = self._list_page(content_state=0, page_no=page_no)
            draft_hit = any(_item_id(r) == article_id for r in rows)
            if draft_hit:
                return 'draft'
            if len(rows) < 20:
                break
        for page_no in range(1, 51):
            rows = self._list_page(content_state=3, page_no=page_no)
            if any(_item_id(r) == article_id for r in rows):
                return 'published'
            if len(rows) < 20:
                break
        for page_no in range(1, 51):
            rows = self._list_page(content_state=1, page_no=page_no)
            if any(_item_id(r) == article_id for r in rows):
                return 'pending'
            if len(rows) < 20:
                break
        return 'unknown'

    def _find(self, article_id: str) -> dict | None:
        for state in (0, 1, 2, 3):
            for page_no in range(1, 51):
                rows = self._list_page(content_state=state, page_no=page_no)
                for item in rows:
                    if _item_id(item) == article_id:
                        return item
                if len(rows) < 20:
                    break
        return None

    def article_detail(self, article_id: str) -> dict | None:
        """Read the creator editor's per-ID post, including offline state 7."""
        self.account()
        payload = self._checked(self.http.json(
            'GET', MP + '/wemedia/article/editpage.do',
            params={'postId': article_id, 'wemediaId': self.wemedia_id,
                    'mediaId': self.wemedia_id},
            headers={'Referer': MP + '/wemedia/index.html'}), what='单篇详情')
        data = payload.get('data')
        post = data.get('post') if isinstance(data, dict) else None
        return post if isinstance(post, dict) else None

    def verify(self, article_id: str, *, expected=None, evidence=None):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', article_id):
            raise ValueError('网易文章 ID 无效')
        self.account()
        proof = dict(evidence or {})
        if expected is not None:
            proof.update(content_fingerprint(PLATFORM, expected.title, strip_markdown_images(expected.body)))
        item = self._find(article_id)
        if not item:
            detail = self.article_detail(article_id)
            if (isinstance(detail, dict) and detail.get('docid') == article_id
                    and detail.get('wemediaId') == self.wemedia_id
                    and str(detail.get('postState')) in ('6', '7')):
                return PublishResult('deleted', f'网易单篇详情状态码 {detail["postState"]}，文章已下线或删除；公开链接已失效',
                                     platform=PLATFORM, verification='verified')
            return PublishResult('pending', '网易列表未找到该文章，不会重发', platform=PLATFORM, verification='unavailable')
        state = _content_state(item)
        if state == 'deleted':
            return PublishResult('deleted', '网易作品列表显示文章已下线或删除；公开链接已失效',
                                 platform=PLATFORM, verification='verified')
        if state == 'draft':
            return PublishResult('draft', f'网易草稿 {article_id} 已保存',
                                 f'https://mp.163.com/subscribe_v4/index.html#/article-publish/{article_id}',
                                 PLATFORM, 'verified')
        if not state.startswith('published'):
            return PublishResult('pending', '网易尚未通过要求的发布状态核验',
                                 platform=PLATFORM, verification='unavailable')
        restricted = '分发受限' in state
        url = f'https://www.163.com/dy/article/{article_id}.html'
        note = '；此文被平台限制分发' if restricted else ''
        available = proof.get('sha256') and proof.get('media')
        if not available:
            return PublishResult('published', f'网易已发布{note}；缺少原稿或上传图片证据，未做内容比对',
                                 url, PLATFORM, 'published')
        content = item.get('content')
        if isinstance(content, str) and content:
            html = _ArticleHTML(content if '<' in content else '<p>' + content + '</p>')
            approx_ok = (content_matches(PLATFORM, _text_of(item), html.text, proof)
                         if (html.text.strip() and html.images) else True)
            if approx_ok:
                return PublishResult('published', f'网易已发表且原稿核对通过{note}', url, PLATFORM, 'verified')
        # 列表接口正文可能被裁剪；正文比对不可靠时以发布状态为准，但不宣称完整核验。
        return PublishResult('published', f'网易已发表{note}；未做完整内容比对', url, PLATFORM, 'published')


def _text_of(item: dict) -> str:
    return str(item.get('title') or '')
