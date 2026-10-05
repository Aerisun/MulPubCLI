"""网易号瞬时浏览器公开发布 — 平台守卫在真实编辑器里校验，浏览器用完即关。

背景：网易 `publishV2.do` 的公开提交（operation=publish）需要 `neg.getToken()` 铸造的
ursToken，只有 v4 真实编辑器 `/article-publish` 在活体会话里能产出来；纯 HTTP 直发不带
ursToken 会被风控打回到受限状态（文章不进已发布列表）。而产出来再回放纯 HTTP 也不可靠。

同时 v4 发布有两道硬校验：正文**至少一张图片**、必须**完成封面图设置**。这两项都只能
通过真实编辑器完成——插图走编辑器自己的 `POST /api/v3/upload/picupload`（把本地图当
图片文件粘贴，编辑器自动上传并插入），封面从「选择封面图」弹窗里选取正文首图确认。

因此本模块先把封面插入正文开头，再按 Markdown 位置逐块粘贴正文文字和图片；
随后核对编辑器实际图文顺序，并选择正文首图作为封面。平台自家守卫
当场铸造并校验 ursToken。

安全：所有请求走 mihomo 代理；会话 Cookie 从项目凭证注入；浏览器瞬启瞬关。
"""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl

from mulpubcli.http import HTTPFailure

from .client import DAILY_QUOTA_MESSAGE, is_daily_quota_rejection

_IMAGE_PASTE_JS = """({b64, name, mime_type}) => {
    const bin = atob(b64);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    const file = new File([arr], name, {type: mime_type});
    const el = document.querySelector('[contenteditable="true"]');
    el.focus();
    const dt = new DataTransfer();
    dt.items.add(file);
    el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
    return true;
}"""


class NeteaseBrowserPublish:
    """驱动 v4 `/article-publish` 编辑器完成一次网易公开投稿。

    Args:
        cookies:     已登录网易号的会话 Cookie 列表（项目凭证）。
        title:       文章标题。
        html:        渲染好的正文 HTML（用于抽取纯文本填入 Draft）。
        body_images: 文章正文的本地图片文件列表；网易发布要求正文至少一张图片。
        body_blocks: Markdown 原始位置对应的文字和图片块，正文带图时必填。
        cover_path:  稿件指定的封面本地路径。
    """

    _URL = 'http://mp.163.com/subscribe_v4/index.html#/article-publish'

    def __init__(self, cookies: list[dict], title: str, html: str, *,
                 body_images: list[Path] | None = None,
                 cover_path: Path | None = None,
                 body_blocks: list[tuple[str, object]] | None = None,
                 dest: Path | None = None, dry_run: bool = False,
                 on_ready=None):
        self.cookies = cookies
        self.title = title
        self.html = html
        self.plain_text = _strip_html(html)
        self.body_images = list(body_images or [])
        self.cover_path = cover_path
        if self.body_images and body_blocks is None:
            raise ValueError('网易正文图片必须提供 Markdown 位置对应的 body_blocks')
        self.body_blocks = body_blocks if body_blocks is not None else \
            ([('text', self.plain_text)] if self.plain_text.strip() else [])
        if self.cover_path is not None and (
                not self.body_images or self.body_images[0] != self.cover_path
                or not self.body_blocks or self.body_blocks[0] != ('image', self.cover_path)):
            raise ValueError('网易封面必须是正文首图，才能从正文图片中选为封面')
        self.dest = dest
        self.dry_run = dry_run
        self.on_ready = on_ready
        self._submission_attempted = False

    # ─────────────────────────────────────────────
    # Playwright 自动化
    # ─────────────────────────────────────────────

    def _browser_cookies(self) -> list[dict]:
        out = []
        for c in self.cookies:
            try:
                out.append({'name': c['name'], 'value': c['value'], 'domain': c.get('domain', ''),
                            'path': c.get('path', '/'), 'secure': bool(c.get('secure', False))})
            except (KeyError, TypeError):
                continue
        return out

    def _focus_editor(self, page) -> None:
        """聚焦正文 Draft 编辑器，把下一次粘贴光标放在现有内容尾部。"""
        page.evaluate("""() => {
            const el = document.querySelector('[contenteditable="true"]');
            if (!el) throw new Error('网易正文编辑器不存在');
            el.focus();
            const range = document.createRange();
            range.selectNodeContents(el);
            range.collapse(false);
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
            return true;
        }""")

    def _paste_text(self, page, text: str) -> None:
        """把正文纯文本以原生 paste 事件填入 Draft（Draft 走文本分支进 editorState）。"""
        self._focus_editor(page)
        page.evaluate("""js => {
            const el = document.activeElement && document.activeElement.isContentEditable
                ? document.activeElement : document.querySelector('[contenteditable="true"]');
            el.focus();
            const dt = new DataTransfer();
            dt.setData('text/plain', js);
            el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
            return true;
        }""", text)
        page.wait_for_timeout(500)

    def _paste_image(self, page, image_path: Path) -> None:
        """把本地图片以图片文件粘贴进正文；编辑器自动 picupload 上传并插入 <img>。

        这是真人插图的标准路径：粘贴带真实图片媒体类型的 ClipboardEvent，网易编辑器识别
        到图片文件后 POST /api/v3/upload/picupload，返回 URL 后插入正文。
        """
        self._focus_editor(page)
        count_js = ("() => { const el=document.querySelector('[contenteditable=true]'); "
                    "return el ? el.querySelectorAll('img').length : 0; }")
        before = page.evaluate(count_js)
        mime_type = mimetypes.guess_type(Path(image_path).name)[0]
        if mime_type not in ('image/jpeg', 'image/png', 'image/webp'):
            raise HTTPFailure(f'网易正文图片格式不支持：{Path(image_path).name}', kind='validation')
        b64 = base64.b64encode(Path(image_path).read_bytes()).decode('ascii')
        page.evaluate(_IMAGE_PASTE_JS, {'b64': b64, 'name': Path(image_path).name,
                                        'mime_type': mime_type})
        # 等编辑器上传并插入图片
        for _ in range(20):
            page.wait_for_timeout(500)
            n = page.evaluate(count_js)
            if isinstance(n, int) and isinstance(before, int) and n > before:
                return
        raise HTTPFailure(f'网易正文图片未插入编辑器：{Path(image_path).name}',
                          kind='invalid_response')

    def _insert_body(self, page) -> None:
        """Paste text and images in source order, waiting for each image insertion."""
        self._n_images = 0
        for kind, value in self.body_blocks:
            if kind == 'text' and value:
                self._paste_text(page, value)
            elif kind == 'image':
                self._paste_image(page, value)
                self._n_images += 1
        if self._n_images == 0:
            raise HTTPFailure('网易发布要求正文至少一张图片，但该文章没有配图',
                              kind='validation')

    def _verify_body_order(self, page) -> None:
        """从编辑器真实 DOM 读取图文顺序，错位时停在提交前。"""
        actual = page.evaluate("""() => {
            const el = document.querySelector('[contenteditable="true"]');
            if (!el) return null;
            const parts = [''];
            const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
            let node;
            while ((node = walk.nextNode())) {
                if (node.nodeType === Node.ELEMENT_NODE && node.tagName === 'IMG') {
                    parts.push('');
                } else if (node.nodeType === Node.TEXT_NODE) {
                    // 图片块是不可编辑的组件，里面的 figcaption 占位提示和按钮文案
                    // 不是文章正文；图片本身仍在上面的分支计入顺序。
                    if (!node.parentElement?.closest('[contenteditable="false"]')) {
                        parts[parts.length - 1] += node.textContent || '';
                    }
                }
            }
            return parts;
        }""")
        expected = ['']
        for kind, value in self.body_blocks:
            if kind == 'image':
                expected.append('')
            elif kind == 'text':
                expected[-1] += str(value)

        def normalize(value: str) -> str:
            return re.sub(r'\s+', '', value).replace('\u200b', '')

        if (not isinstance(actual, list) or len(actual) != len(expected)
                or any(not isinstance(part, str) or normalize(part) != normalize(want)
                       for part, want in zip(actual, expected))):
            raise HTTPFailure('网易编辑器中的图文顺序与 Markdown 不一致，已停止发布',
                              kind='validation')

    def _set_cover(self, page, ctx, browser) -> None:
        """设置封面：切到「单图」封面模式 → 点上传槽 → 弹窗选正文图 → 确认。

        网易 v4 封面按模式区分：默认「三图」需选 3 张正文图才解禁确认；「单图」只需
        1 张。本方法把封面模式切到单图，点 `.cover-pic__single__content__choose`
        上传槽弹出「选择封面图」，点第一个 `.cover-picture__item`，确认（选中后解禁）
        即把正文图设为封面。
        """
        # 封面必须为正文图；切「单图」模式（三图需 3 张、单图只需 1 张）。
        try:
            page.get_by_text('单图', exact=True).first.click()
            page.wait_for_timeout(1200)
        except Exception as exc:
            raise HTTPFailure('网易单图封面模式未能开启', kind='validation') from exc
        slot = page.query_selector('.cover-pic__single__content__choose') \
            or page.query_selector('.cover-pic__content__choose')
        if slot is None:
            raise HTTPFailure('网易封面选择入口不存在', kind='validation')
        try:
            slot.click()
        except Exception as exc:
            raise HTTPFailure('网易封面选择入口无法打开', kind='validation') from exc
        # 「选择封面图」弹窗候选异步渲染：轮询第一个封面 item（最长约 8s）。
        item = None
        for _ in range(16):
            page.wait_for_timeout(500)
            try:
                item = page.query_selector('.cover-picture__item')
            except Exception:
                item = None
            if item is not None:
                break
        if item is None:
            raise HTTPFailure('网易封面候选图片未出现', kind='validation')
        try:
            item.click()
            page.wait_for_timeout(600)
            confirmed = page.evaluate("""() => {
                const f = document.querySelector('.cover-picture__footer');
                if (!f) return false;
                const d = [...f.querySelectorAll('div')]
                    .find(x => (x.innerText || '').trim() === '确认');
                if (d && !/disabled/.test(d.className)) { d.click(); return true; }
                return false;
            }""")
            if not confirmed:
                raise HTTPFailure('网易封面未通过候选图片确认', kind='validation')
            page.wait_for_timeout(2500)
        except HTTPFailure:
            raise
        except Exception as exc:
            raise HTTPFailure('网易封面选择失败', kind='validation') from exc

    def _perform(self, page, browser, ctx) -> bool:
        """填稿并点发布；返回 True=需要人工（异常时），False=已提交完成。"""
        # PlaywrightLoginer 的 context 是空 profile：先注入会话 Cookie 再进编辑器。
        page.context.add_cookies(self._browser_cookies())
        page.goto(self._URL, timeout=40_000, wait_until='domcontentloaded')
        page.wait_for_timeout(4000)
        # 编辑器需登录态加载；若被带到 lead/登录页则回编辑器路由。
        for _ in range(4):
            try:
                page.wait_for_selector('textarea.netease-textarea', timeout=15_000)
                break
            except Exception:
                page.goto(self._URL, timeout=30_000, wait_until='domcontentloaded')
                page.wait_for_timeout(4000)
        else:
            raise HTTPFailure('网易 v4 编辑器未能加载（可能被带离编辑器路由）', kind='invalid_response')
        page.wait_for_selector('[contenteditable="true"]', timeout=30_000)
        page.wait_for_timeout(2500)
        # 标题
        page.fill('textarea.netease-textarea', self.title)
        # 正文：按文档原始顺序交替贴文字/插图（先贴全部文字、图全沉底，顺序会错乱）。
        self._insert_body(page)
        page.wait_for_timeout(1500)
        # 封面（从正文图选择）
        self._set_cover(page, ctx, browser)
        page.wait_for_timeout(1200)
        self._verify_body_order(page)
        # dry-run：只填充不提交，供校验与人工核对。
        if self.dry_run:
            preview = page.evaluate("""() => {
                const t=document.querySelector('textarea.netease-textarea');
                const c=document.querySelector('[contenteditable="true"]');
                // 封面图可能落在单图槽或通用封面位；取任一存在的已上传 img
                let cover='none';
                for (const sel of ['.cover-pic__single img','.cover-pic__cover img','.cover-pic__single__content img']) {
                    const img=document.querySelector(sel);
                    if (img && (img.src||'').indexOf('dingyue')>=0) { cover=img.src.slice(0,60); break; }
                }
                return {title:t?t.value:'', len:c?(c.innerText||'').length:0,
                        images:c?c.querySelectorAll('img').length:0,
                        text:(c?c.innerText:'').slice(0,60),
                        cover:cover};
            }""")
            if self.on_ready:
                self.on_ready(preview)
            return False
        return self._submit_editor(page)

    def _submit_editor(self, page) -> bool:
        """Submit from the editor; either click may already finish the publication."""
        # 发布：网易 v4 需「点两次」。第一次点「发布」触发标题/正文/封面诊断
        # （发文助手 handleDigest，含 checkTitle、content/coverPic 检查、错别字检测），
        # 诊断通过后 isFirstDigest 置否；第二次点才真正走 convertPublishParams→publishV2.do。
        # 若当日发布额度已满（6/天），点「发布」会弹出「今日发布数量已达最大值」。
        btn = 'button.ne-button-color-primary[class*=primary]'
        pv2 = []
        # response 事件只记录对象；在回调中读 body 可能遇到页面跳转而漏掉成功响应。
        def remember_publish(response):
            if 'publishV2.do' not in response.url:
                return
            request = getattr(response, 'request', None)
            params = dict(parse_qsl(getattr(request, 'post_data', '') or ''))
            if params.get('operation') == 'publish':
                pv2.append(response)

        page.on('response', remember_publish)

        def accepted_response():
            if not pv2:
                return None
            try:
                body = pv2[-1].text()[:600]
            except Exception:
                return None
            code = _publish_code(body)
            if code != 1:
                if is_daily_quota_rejection(body):
                    raise HTTPFailure(DAILY_QUOTA_MESSAGE, kind='limit', code=code)
                raise HTTPFailure(f'网易发布被平台拒绝（code={code}）：{body[:200]}',
                                  kind='invalid_response')
            from .client import _doc_id
            try:
                self.submitted_id = _doc_id(json.loads(body).get('data'))
            except (ValueError, TypeError):
                self.submitted_id = ''
            return False

        self._submission_attempted = True
        try:
            page.click(btn, timeout=8000)
        except Exception:
            confirmed = accepted_response()
            if confirmed is not None:
                return confirmed
            try:
                page.click('button:has-text("发布")', timeout=8000)
            except Exception:
                if self._quota_hit(page, checks=1):
                    raise HTTPFailure(DAILY_QUOTA_MESSAGE, kind='limit')
                return True   # 没点成，需要人工介入
        page.wait_for_timeout(1500)
        confirmed = accepted_response()
        if confirmed is not None:
            return confirmed
        # 第一次点：等诊断/助手面板 settle；期间若弹出额度已满则明确报错而非空等。
        quota = self._quota_hit(page)
        if quota:
            raise HTTPFailure(DAILY_QUOTA_MESSAGE, kind='limit')
        page.wait_for_timeout(6000)
        confirmed = accepted_response()
        if confirmed is not None:
            return confirmed
        # 第二次点：真正提交 publishV2。
        try:
            page.click(btn, timeout=8000)
        except Exception:
            confirmed = accepted_response()
            if confirmed is not None:
                return confirmed
            try:
                page.click('button:has-text("发布")', timeout=8000)
            except Exception:
                if self._quota_hit(page, checks=1):
                    raise HTTPFailure(DAILY_QUOTA_MESSAGE, kind='limit')
                return True
        # 等待 publishV2 返回（最长约 20 秒）。
        deadline = time.time() + 20
        while time.time() < deadline and not pv2:
            if self._quota_hit(page, checks=1):
                raise HTTPFailure(DAILY_QUOTA_MESSAGE, kind='limit')
            page.wait_for_timeout(1000)
        confirmed = accepted_response()
        if confirmed is not None:
            return confirmed
        # 无 publishV2 响应：可能是二次确认弹窗（如无图确认/额度）未处理，需人工。
        if self._quota_hit(page, checks=1):
            raise HTTPFailure(DAILY_QUOTA_MESSAGE, kind='limit')
        return True

    def _published_ids(self) -> set[str] | None:
        """Read published IDs for this exact title; None means list evidence is unavailable."""
        if not self.dest or not self.dest.is_file():
            return None
        from .client import NeteaseWeb
        client = None
        try:
            client = NeteaseWeb.load(self.dest)
            rows = client.list_articles()
            if not getattr(client, '_last_list_complete', False):
                return None
            return {row['id'] for row in rows if row.get('title') == self.title
                    and str(row.get('status', '')).startswith('published') and row.get('id')}
        except Exception:
            return None
        finally:
            if client is not None:
                client.close()

    def _quota_hit(self, page, *, checks: int = 10) -> bool:
        """点「发布」后检测当日额度已满弹窗（「今日发布数量已达最大值」）。"""
        for _ in range(checks):
            page.wait_for_timeout(500)
            hit = page.evaluate("""() => {
                for (const m of document.querySelectorAll('.custom-confirm,.ne-modal,[class*=modal-],[role="dialog"]')) {
                    if (m.offsetParent === null) continue;
                    if ((m.innerText || '').indexOf('今日发布数量已达最大值') >= 0) return true;
                }
                return false;
            }""")
            if hit:
                return True
        return False

    def run(self, *, headless: bool = True, timeout_ms: int = 120_000) -> dict:
        """执行一次瞬时浏览器发布；浏览器用完即关。"""
        from mulpubcli.browser import LoginResult, PlaywrightLoginer
        from .client import USER_AGENT

        before_ids = self._published_ids() if not self.dry_run else None
        loginer = PlaywrightLoginer(user_agent=USER_AGENT,
                                    storage_state_dir=self.dest.parent if self.dest
                                    else Path('.cache'))
        try:
            result = loginer.login(self._URL, self._perform, headless=headless,
                                   max_human_wait_ms=timeout_ms, viewport=(1600, 1000))
        except Exception as exc:
            if not self._submission_attempted or (isinstance(exc, HTTPFailure)
                                                  and exc.kind in ('validation', 'limit')):
                raise
            # 页面在提交后跳转/关闭时，Playwright 也可能抛错；先回查，不据此断言失败。
            result = LoginResult(status='need_human', message='网易提交后浏览器状态不明')
        if self.dry_run:
            return {'status': 'prepared', 'platform': 'netease', 'message': result.message}
        if self._submission_attempted and (result.status != 'ok' or not getattr(self, 'submitted_id', '')):
            after_ids = self._published_ids()
            new_ids = after_ids - before_ids if before_ids is not None and after_ids is not None else set()
            if len(new_ids) == 1:
                self.submitted_id = new_ids.pop()
                return {'status': 'published', 'platform': 'netease',
                        'message': '已从网易已发布列表确认', 'remote_id': self.submitted_id}
            if result.status != 'ok' or not getattr(self, 'submitted_id', ''):
                return {'status': 'pending', 'platform': 'netease',
                        'message': '网易提交结果未确认，请核验已发布列表；不要重发'}
        if result.status != 'ok':
            return {'status': result.status, 'platform': 'netease', 'message': result.message}
        return {'status': 'ok', 'platform': 'netease', 'message': '网易公开投稿已进入平台流程',
                'remote_id': getattr(self, 'submitted_id', ''), 'browser': True}


__all__ = ['NeteaseBrowserPublish']


def _publish_code(body: str) -> int:
    """从 publishV2.do 响应体里取业务 code；1 表示成功，其余为平台拒绝。"""
    try:
        return int(json.loads(body).get('code', 0))
    except Exception:
        return 0


def _strip_html(html: str) -> str:
    """把渲染好的正文 HTML 抽成纯文本，供 Draft 粘贴路径使用。

    Draft 会把粘贴内容按纯文本归一（正文图片由 _paste_image 逐个上传），这里只保留可读文本。
    """
    html = re.sub(r'<(br|/p|/div|/li)>', '\n', html, flags=re.I)
    html = re.sub(r'<[^>]+>', '', html)
    text = re.sub(r'\n[ \t]*\n+', '\n\n', html)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()
