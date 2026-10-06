"""XHS Creator HTTP requests. The local JS signer is an explicitly pinned source checkout."""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import re
import secrets
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from PIL import Image

from mulpubcli.core import Article, PublishResult, content_fingerprint, content_matches
from mulpubcli.renderer import plain_markdown_text
from mulpubcli.http import HTTP, HTTPFailure, private_json
from .session import SIGNER_REV, load_source, restore_profile, dump_profile
from .signing import creator_params


def upload_signature(period: str, file_id: str, size: int, host: str) -> str:
    key = hmac.new(b'null', period.encode(), hashlib.sha1).hexdigest()
    canonical = f'put\n/spectrum/{file_id}\n\ncontent-length={size}&host={host}\n'
    message = f'sha1\n{period}\n{hashlib.sha1(canonical.encode()).hexdigest()}\n'
    return hmac.new(key.encode(), message.encode(), hashlib.sha1).hexdigest()


def pixel_fingerprint(source) -> str:
    """Hash decoded RGB pixels so CDN image IDs and PNG metadata do not matter."""
    with Image.open(source) as image:
        rgb = image.convert('RGB')
        digest = hashlib.sha256(f'{rgb.width}x{rgb.height}:'.encode())
        digest.update(rgb.tobytes())
        return digest.hexdigest()


def image_profile(path: Path) -> list[int]:
    """Dimensions and exact normalized upload size, in original image order."""
    with Image.open(path) as image:
        output = io.BytesIO()
        image.convert('RGB').save(output, 'PNG')
        return [image.width, image.height, len(output.getvalue())]


def image_payload(article: Article, images: list[dict]) -> dict:
    binds = {'version': 1, 'noteId': 0, 'bizType': 0, 'noteOrderBind': {},
             'notePostTiming': {}, 'noteCollectionBind': {'id': ''},
             'noteSketchCollectionBind': {'id': ''}, 'coProduceBind': {'enable': True},
             'noteCopyBind': {'copyable': True}, 'interactionPermissionBind': {'commentPermission': 0},
             'optionRelationList': []}
    return {'common': {
        'type': 'normal', 'title': article.title, 'desc': plain_markdown_text(article.body), 'note_id': '',
        'source': json.dumps({'type': 'web', 'ids': '', 'extraInfo': json.dumps({'subType': 'official', 'systemId': 'web'})}),
        'business_binds': json.dumps(binds, separators=(',', ':')), 'ats': [], 'hash_tag': [],
        'privacy_info': {'op_type': 1, 'type': 0, 'user_ids': []}, 'goods_info': {}, 'biz_relations': [],
        'capa_trace_info': {'contextJson': json.dumps({'recommend_title': {'recommend_title_id': '', 'is_use': 3, 'used_index': -1},
                                                    'recommendTitle': [], 'recommend_topics': {'used': []}})},
    }, 'image_info': {'images': [{
        'file_id': 'spectrum/' + uploaded['file_id'], 'width': uploaded['width'], 'height': uploaded['height'],
        'metadata': {'source': -1}, 'stickers': {'version': 2, 'floating': []},
        'extra_info_json': json.dumps({'mimeType': 'image/png', 'image_metadata': {'bg_color': '', 'origin_size': uploaded['size'] / 1024}}),
    } for uploaded in images]}, 'video_info': None}


def note_url(note_id: str, token: str = '', source: str = '') -> str:
    """Return the creator feed's openable share URL when its access token is present."""
    if not re.fullmatch(r'[0-9a-f]{24}', note_id):
        raise ValueError('小红书笔记 ID 无效')
    url = f'https://www.xiaohongshu.com/explore/{note_id}'
    if token:
        params = {'xsec_token': token}
        if source:
            params['xsec_source'] = source
        url += '?' + urlencode(params)
    return url


class XHSHTTP:
    BASE = 'https://creator.xiaohongshu.com'
    PUBLISH = 'https://edith.xiaohongshu.com'

    def __init__(self, credentials: Path, *, source: Path):
        source = load_source(source)
        if credentials.stat().st_mode & 0o077:
            raise ValueError('凭证文件须为 600 权限')
        config = json.loads(credentials.read_text())
        cookies = config.get('cookie', '')
        if not cookies:
            # A platform-specific HTTP Cookie header export may also use an array.
            allowed = [c for c in config.get('cookies', []) if c.get('domain', '').lstrip('.') in ('xiaohongshu.com', 'creator.xiaohongshu.com')]
            cookies = '; '.join(c['name'] + '=' + c['value'] for c in allowed)
        from xhs_utils.xhs_creator.auth import XHSCreatorAuth
        self.source, self.sign = source, creator_params
        signing = dict(config.get('signing', {}))
        if config.get('profile'):
            signing['profile'] = restore_profile(config['profile'])
        self.auth = XHSCreatorAuth.from_cookie(cookies, **signing)
        self.credentials = credentials
        self.http = HTTP({'creator.xiaohongshu.com', 'edith.xiaohongshu.com'}, session=self.auth.http_client,
                         on_response=self._remember_response)
        self.temporary = source.parent.parent / 'tmp'
        self.temporary.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.last_timestamp = ''

    def _remember_response(self, response):
        values = self.auth.profile.cookie_map
        self.auth._cookie_store.merge_response(values, response)
        self.auth.update_cookies(values, source_url=response.url)
        self.save(response=response)

    def save(self, *, response=None):
        import datetime, json
        from mulpubcli.login_flow import cookie_expiry_metadata, response_cookie_expirations
        data = {}
        if self.credentials.exists():
            try:
                data = json.loads(self.credentials.read_text())
            except Exception:
                pass
        data.update({'cookie': self.auth.cookies, 'profile': dump_profile(self.auth.profile),
            'signing': {'host_cookie_state': self.auth._cookie_store.export_state()}})
        if 'cookie_expirations' not in data:
            # Older credentials used a guessed one-year expiry; it was not a
            # server Cookie attribute and must not be presented as one.
            data['expires_at'] = None
            data['cookie_expirations'] = {}
        else:
            data.setdefault('expires_at', None)
        if response is not None:
            changes = response_cookie_expirations(response)
            if changes:
                expirations = dict(data['cookie_expirations'])
                expirations.update(cookie_expiry_metadata(changes, auth_names=())['cookie_expirations'])
                data['cookie_expirations'] = expirations
                data['expires_at'] = expirations.get('web_session')
        data['updated_at'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        private_json(self.credentials, data)

    def call(self, method: str, path: str, payload=None, *, publish=False, edith=False, cold=False):
        origin = self.PUBLISH if publish or edith else self.BASE
        options = {'tier': '0101', 'b1_profile': 'note_manager', 'mns_profile': 'note_manager'} if cold else {}
        old_tempdir = tempfile.tempdir
        try:
            tempfile.tempdir = str(self.temporary)
            headers, cookies, body = self.sign(self.auth, path, payload if payload is not None else '', method,
                include_client_hints=False, **options)
        finally:
            tempfile.tempdir = old_tempdir
        if publish:
            # Use stdin so article content never appears in the process command line.
            module = self.source / 'xhs_utils/xhs_pc/js/rap.js'
            template = self.source / 'xhs_utils/xhs_creator/js/rap_fingerprint_creator.json'
            raw = bytearray.fromhex(json.loads(template.read_text())['bodyUnmaskedHex'])
            raw[6:22] = secrets.token_hex(8).encode()
            script = "const fs=require('fs');const x=JSON.parse(fs.readFileSync(0,'utf8'));const {buildRapPure}=require(process.argv[1]);process.stdout.write(buildRapPure({api:x.api,data:x.body,fingerprint:Buffer.from(x.fingerprint,'hex')}));"
            # Creator RAP hashes the absolute HTTPS URL, including query, with the exact wire body.
            proc = subprocess.run(['node', '-e', script, str(module)], input=json.dumps({'api': origin + path, 'body': body, 'fingerprint': raw.hex()}),
                                  capture_output=True, text=True, timeout=30)
            if proc.returncode or not proc.stdout.startswith('ByQ'):
                raise HTTPFailure('小红书发布签名生成失败')
            headers['x-rap-param'] = proc.stdout.strip()
        self.last_timestamp = str(headers['x-t'])
        cookies = self.auth.cookies_for_url(origin + path, cookies)
        response = self.http.json(method, origin + path, headers=headers, cookies=cookies,
                                  **({'data': body.encode()} if method != 'GET' else {}))
        if response.get('success') is not True:
            code = response.get('code', response.get('result'))
            code = code if type(code) is int else 'unknown'
            raise HTTPFailure(f'小红书拒绝请求（code={code}），已停止且不重试',
                              code=code if type(code) is int else None)
        return response

    def posted(self, page=0):
        if type(page) is not int or page < 0:
            raise ValueError('小红书列表页码无效')
        return self.call('GET', f'/api/galaxy/v2/creator/note/user/posted?tab=0&page={page}', cold=True)

    def detail(self, note_id: str):
        if not re.fullmatch(r'[0-9a-f]{24}', note_id):
            raise ValueError('小红书笔记 ID 无效')
        return self.call('GET', f'/web_api/sns/capa/postgw/note/detail?note_id={note_id}&source=web&edit_mode=1', edith=True)

    @staticmethod
    def _remote_image_hash(url: str) -> str:
        try:
            parsed = urlsplit(url)
            host = parsed.hostname or ''
            if (parsed.scheme != 'https' or not host.endswith('.xhscdn.com') or
                    parsed.username or parsed.password or parsed.port not in (None, 443)):
                raise ValueError('bad image URL')
        except (TypeError, ValueError):
            raise HTTPFailure('小红书原图地址无效，图片核验未完成', kind='invalid_response') from None
        response = HTTP({host}).request('GET', url, stream=True)
        try:
            chunks, size = [], 0
            for chunk in response.iter_content(1024 * 1024):
                size += len(chunk)
                if size > 25 * 1024 * 1024:
                    raise HTTPFailure('小红书原图超过核验大小上限', kind='invalid_response')
                chunks.append(chunk)
            try:
                return pixel_fingerprint(io.BytesIO(b''.join(chunks)))
            except (OSError, ValueError):
                raise HTTPFailure('小红书原图无法解码，图片核验未完成', kind='invalid_response') from None
        finally:
            response.close()

    def statuses(self):
        """Follow the creator feed cursor; report whether its end was reached."""
        fields = ('id', 'display_title', 'tab_status', 'permission_code', 'time',
                  'xsec_token', 'xsec_source')
        notes, visited, page = [], set(), 0
        for _ in range(20):
            if page in visited:
                return {'notes': notes, 'complete': False}
            visited.add(page)
            data = self.posted(page=page).get('data')
            if not isinstance(data, dict) or not isinstance(data.get('notes'), list):
                raise HTTPFailure('小红书作品列表结构变化，停止回查', kind='invalid_response')
            if any(not isinstance(note, dict) for note in data['notes']):
                raise HTTPFailure('小红书作品条目结构变化，停止回查', kind='invalid_response')
            notes.extend({key: note.get(key) for key in fields} for note in data['notes'])
            next_page = data.get('page')
            if not data['notes'] or next_page in (None, 0, -1):
                return {'notes': notes, 'complete': True}
            if type(next_page) is not int or next_page < 0:
                raise HTTPFailure('小红书作品分页标记无效，停止回查', kind='invalid_response')
            page = next_page
        return {'notes': notes, 'complete': False}

    def verify(self, note_id: str, *, expected: Article | None = None, evidence=None) -> PublishResult:
        if not re.fullmatch(r'[0-9a-f]{24}', note_id):
            raise ValueError('小红书笔记 ID 无效')
        evidence = dict(evidence or {})
        if expected is not None:
            evidence.update(content_fingerprint('xiaohongshu', expected.title, plain_markdown_text(expected.body)))
            evidence['media_pixels'] = [pixel_fingerprint(path) for path in expected.all_images()]
            evidence['media_profiles'] = [image_profile(path) for path in expected.all_images()]
        url = None
        verification = 'unavailable'
        listing = self.statuses()
        note = next((item for item in listing['notes'] if item.get('id') == note_id), None)
        if note is None and listing['complete']:
            try:
                self.detail(note_id)
            except HTTPFailure as exc:
                if exc.code == -9106:
                    return PublishResult('deleted', '小红书单篇详情返回 -9106：该笔记已被删除；公开链接已失效',
                                         platform='xiaohongshu', verification='verified')
                raise
        # 小红书公开笔记要带 xsec_token（必要时 xsec_source）才能直接打开，否则匿名访问
        # 落到 404 拦截页。发布列表卡片里带这两个字段，抓到后拼成完整分享链接。
        if note:
            token = note.get('xsec_token')
            if token:
                url = note_url(note_id, token, note.get('xsec_source') or '')
        status, message = 'pending', ('完整作品列表未找到该笔记，尚不能判定删除' if listing['complete']
                                      else '已检查作品列表未找到该笔记；分页未结束，需继续核验')
        if note:
            tab = note.get('tab_status')
            if tab == 3:
                status, message = 'failed', '平台显示审核未通过；请处理原因，不会自动重发'
            elif tab == 2:
                message = '平台显示审核中，不会重发'
            elif tab == 1 and note.get('permission_code') == 0:
                data = self.detail(note_id).get('data') or {}
                if not isinstance(data, dict):
                    raise HTTPFailure('小红书详情结构变化', kind='invalid_response')
                if data.get('id') == note_id and data.get('enabled') is True and (data.get('privacy') or {}).get('type') == 0:
                    images = data.get('images_list')
                    media = [item.get('fileid', '').removeprefix('spectrum/') for item in images
                             if isinstance(item, dict) and isinstance(item.get('fileid'), str)] if isinstance(images, list) else []
                    expected_media = evidence.get('media')
                    media_match = not expected_media or media == expected_media
                    media_unavailable = False
                    media_partial = False
                    if expected_media and not media_match:
                        pixels = evidence.get('media_pixels')
                        if (isinstance(images, list) and isinstance(pixels, list)
                                and len(pixels) == len(media) == len(images)
                                and all(isinstance(value, str) and value for value in pixels)):
                            originals = [item.get('original') for item in images]
                            if all(isinstance(value, str) and value for value in originals):
                                try:
                                    media_match = [self._remote_image_hash(value) for value in originals] == pixels
                                except HTTPFailure:
                                    media_unavailable = True
                            else:
                                media_unavailable = True
                        else:
                            media_unavailable = True
                        if not media_match and media_unavailable:
                            profiles = evidence.get('media_profiles')
                            try:
                                actual_profiles = [[item['width'], item['height'],
                                                    round(item['metadata']['origin_size'] * 1024)]
                                                   for item in images] if isinstance(images, list) else []
                            except (KeyError, TypeError, ValueError):
                                actual_profiles = []
                            if (isinstance(profiles, list) and len(profiles) == len(actual_profiles)
                                    and all(isinstance(profile, list) and len(profile) == 3 for profile in profiles)
                                    and actual_profiles == profiles and len({tuple(p) for p in profiles}) == len(profiles)):
                                media_partial, media_unavailable = True, False
                            else:
                                media_unavailable = True
                    if ((evidence.get('sha256') and not content_matches('xiaohongshu', data.get('title', ''), data.get('desc', ''), evidence))
                            or (expected_media and not media_match and not media_partial and not media_unavailable)):
                        message, verification = '文章存在，但标题、完整正文或上传图片不一致；停止重发', 'mismatch'
                    elif media_partial and evidence.get('sha256'):
                        status, verification = 'published', 'published'
                        message = '平台显示公开可见，标题正文及图片顺序、尺寸和上传大小一致；图片 ID 已变更，原图暂不可下载，未做像素核对'
                    elif media_unavailable:
                        message, verification = '平台已更换图片 ID，缺少原图指纹，暂不能完成图片核验', 'unavailable'
                    elif not evidence.get('sha256') or not evidence.get('media'):
                        # 平台已确认公开可见（审核通过且对外可见），此时状态本身就是发布的确证；
                        # 本地缺原稿/配图证据只意味着不做内容比对，仍应确认发布状态并回填公开链接。
                        status, message, verification = 'published', '平台显示公开可见；缺少原稿或上传图片证据，未做内容比对', 'published'
                    else:
                        status, verification = 'published', 'verified'
                        message = '平台显示公开可见，标题、完整正文和上传图片均通过回读核验；未验证其他账号的展示'
                elif data.get('id') == note_id and (data.get('enabled') is False or (data.get('privacy') or {}).get('type') not in (None, 0)):
                    message, verification = '平台详情显示文章不是公开状态', 'mismatch'
            elif note.get('permission_code') not in (None, 0):
                message, verification = '平台列表显示文章不是公开状态', 'mismatch'
            else:
                message = '尚未同时确认已发布和公开可见'
        return PublishResult(status, message, url=url, platform='xiaohongshu', verification=verification)

    def upload_image(self, path: Path):
        with Image.open(path) as image:
            width, height = image.size
            output = io.BytesIO()
            image.convert('RGB').save(output, 'PNG')
        data = output.getvalue()
        if len(data) > 20 * 1024 * 1024:
            raise ValueError('小红书图片超过 20MB')
        permit = self.call('GET', '/api/media/v1/upload/creator/permit?biz_name=spectrum&scene=image&file_count=1&version=1&source=web', cold=True)
        info = permit['data']['uploadTempPermits'][0]
        host = urlsplit('https://' + info['uploadAddr'].removeprefix('https://')).hostname or ''
        if not (host.endswith('.xiaohongshu.com') or host.endswith('.xhscdn.com')):
            raise HTTPFailure('小红书返回了未获允许的上传域名')
        file_id = info['fileIds'][0].split('/')[-1]
        if not re.fullmatch(r'[A-Za-z0-9_-]+', file_id):
            raise HTTPFailure('小红书上传文件标识无效')
        period = f"{self.last_timestamp[:10]};{str(info['expireTime'])[:10]}"
        signature = upload_signature(period, file_id, len(data), host)
        authorization = f'q-sign-algorithm=sha1&q-ak=null&q-sign-time={period}&q-key-time={period}&q-header-list=content-length;host&q-url-param-list=&q-signature={signature}'
        HTTP({host}).request('PUT', f'https://{host}/spectrum/{file_id}', data=data, headers={
            'Authorization': authorization, 'x-cos-security-token': info['token'],
            'Origin': self.BASE, 'Referer': self.BASE + '/', 'Content-Type': 'image/png',
        })
        return {'file_id': file_id, 'width': width, 'height': height, 'size': len(data),
                'pixel_sha256': pixel_fingerprint(path), 'profile': [width, height, len(data)]}

    def publish(self, article: Article, *, checkpoint=None) -> PublishResult:
        if len(article.title) > 20 or len(plain_markdown_text(article.body)) > 1000:
            return PublishResult('failed', '小红书普通图文限标题 20 字、正文 1000 字；请改稿，不会自动截断', platform='xiaohongshu')
        uploads: list[dict] = []
        try:
            uploads.append(self.upload_image(article.cover))
            for img_path in article.body_images:
                uploads.append(self.upload_image(img_path))
        except Exception as exc:
            return PublishResult('failed', f'小红书上传阶段停止：{type(exc).__name__}', platform='xiaohongshu')
        if checkpoint:
            for uploaded in uploads:
                image_proof = ({'id': uploaded['file_id'],
                                'pixel_sha256': uploaded.get('pixel_sha256'),
                                'profile': uploaded.get('profile')}
                               if uploaded.get('pixel_sha256') else uploaded['file_id'])
                checkpoint('uploaded', image_proof)
        try:
            data = self.call('POST', '/web_api/sns/v2/note', image_payload(article, uploads), publish=True)
        except Exception as exc:
            return PublishResult('pending', f'小红书提交结果需核验：{type(exc).__name__}；不会自动重发', platform='xiaohongshu')
        identifiers = data.get('data') or {}
        note_id = str(identifiers.get('id', identifiers.get('note_id', '')))
        url = f'https://www.xiaohongshu.com/explore/{note_id}' if re.fullmatch(r'[0-9a-f]{24}', note_id) else None
        if url and checkpoint:
            checkpoint('submitted', note_id)
        if url:
            try:
                return self.verify(note_id, expected=article, evidence={'media': [up['file_id'] for up in uploads]})
            except Exception as exc:
                return PublishResult('pending', f'小红书已接受提交，回读暂未完成：{type(exc).__name__}；不会重发', platform='xiaohongshu')
        return PublishResult('pending', '小红书接口已接受提交；公开可见性和审核状态仍需核验', platform='xiaohongshu')
