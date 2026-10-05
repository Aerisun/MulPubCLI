"""Short-lived, loopback-only view into the existing Sohu browser session.

One WebSocket carries cropped JPEG frames and pointer events. Playwright stays
on the login thread; the socket thread only transports bytes and queues input.
"""
from __future__ import annotations

import io
import json
import math
import queue
import secrets
import threading
import time
from urllib.parse import urlsplit


_PAGE = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>搜狐页面验证</title>
<style>
html,body{margin:0;padding:0;width:fit-content;height:fit-content;background:transparent;overflow:hidden}
main{width:360px;max-width:100vw;margin:0;padding:0}
#view{display:block;width:100%;height:auto;touch-action:none;
user-select:none;-webkit-user-drag:none;cursor:grab}
#view:active{cursor:grabbing}
</style><main><img id="view" alt="搜狐验证码窗口" draggable="false"></main>
<script>
const base=location.pathname.endsWith('/')?location.pathname:location.pathname+'/';
const view=document.getElementById('view'),root=document.querySelector('main');
const socket=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+base+'ws');
socket.binaryType='blob';
let dragging=false,lastMove=0,shown=null,pending=null,reportedWidth=0,reportedHeight=0;
function notify(message){
  if(window.parent!==window)window.parent.postMessage(message,location.origin);
  if(window.opener)window.opener.postMessage(message,location.origin);
}
socket.onmessage=e=>{
  if(typeof e.data==='string'){
    const s=JSON.parse(e.data);
    if(s.status==='sent')notify({type:'mulpubcli:sohu:sms-sent'});
    return;
  }
  const next=URL.createObjectURL(e.data);
  if(pending)URL.revokeObjectURL(pending);
  pending=next;
  view.onload=()=>{root.style.width=view.naturalWidth+'px';
    if(view.naturalWidth!==reportedWidth||view.naturalHeight!==reportedHeight){
      reportedWidth=view.naturalWidth;reportedHeight=view.naturalHeight;
      notify({type:'mulpubcli:sohu:challenge-size',
        width:reportedWidth,height:reportedHeight});
    }
    if(shown)URL.revokeObjectURL(shown);shown=next;pending=null};
  view.src=next;
};
function send(type,e){
  if(socket.readyState!==WebSocket.OPEN)return;
  const r=view.getBoundingClientRect();
  if(!r.width||!r.height)return;
  socket.send(JSON.stringify({type,
    x:Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),
    y:Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))}));
}
view.addEventListener('pointerdown',e=>{e.preventDefault();dragging=true;
  view.setPointerCapture(e.pointerId);send('down',e)});
view.addEventListener('pointermove',e=>{if(!dragging)return;e.preventDefault();
  const now=performance.now();if(now-lastMove<30)return;lastMove=now;send('move',e)});
function release(e){if(!dragging)return;e.preventDefault();dragging=false;send('up',e)}
view.addEventListener('pointerup',release);view.addEventListener('pointercancel',release);
</script></html>'''


class BrowserMirror:
    """Temporary mini page for a person to control one live Playwright page."""

    def __init__(
        self, *, port: int = 0, cancel_event: threading.Event | None = None
    ):
        self._requested_port = port
        self._cancel_event = cancel_event
        self._token = secrets.token_urlsafe(24)
        self._events: queue.Queue[tuple[str, float, float]] = queue.Queue(maxsize=512)
        self._frame = b''
        self._frame_seq = 0
        self._frame_lock = threading.Lock()
        self._status = 'waiting'
        self._connected = threading.Event()
        self._clients_lock = threading.Lock()
        self._client_count = 0
        self._server = None
        self._thread: threading.Thread | None = None
        self._clip: dict | None = None
        self._dragging = False

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError('镜像服务尚未启动')
        return self._server.socket.getsockname()[1]

    @property
    def url(self) -> str:
        return f'http://127.0.0.1:{self.port}/t/{self._token}/'

    @staticmethod
    def _valid_event(raw) -> tuple[str, float, float] | None:
        try:
            event = json.loads(raw)
            kind, x, y = event['type'], event['x'], event['y']
            if (kind not in ('move', 'down', 'up') or
                    type(x) not in (int, float) or type(y) not in (int, float) or
                    not math.isfinite(x) or not math.isfinite(y) or
                    not (0 <= x <= 1 and 0 <= y <= 1)):
                return None
            return kind, float(x), float(y)
        except (ValueError, KeyError, TypeError):
            return None

    def __enter__(self):
        from websockets.exceptions import ConnectionClosed
        from websockets.sync.server import serve

        mirror = self
        prefix = f'/t/{self._token}/'

        def process_request(connection, request):
            path = urlsplit(request.path).path
            if path == prefix:
                response = connection.respond(200, _PAGE)
                response.headers['Content-Type'] = 'text/html; charset=utf-8'
                response.headers['Cache-Control'] = 'no-store'
                response.headers['Referrer-Policy'] = 'no-referrer'
                response.headers['X-Content-Type-Options'] = 'nosniff'
                return response
            if path == prefix + 'ws':
                return None
            return connection.respond(404, '')

        def handler(connection):
            with mirror._clients_lock:
                mirror._client_count += 1
                mirror._connected.set()
            last_seq = -1
            last_status = None
            try:
                while True:
                    if mirror._status != last_status:
                        last_status = mirror._status
                        connection.send(json.dumps({'status': last_status}))
                    with mirror._frame_lock:
                        seq, frame = mirror._frame_seq, mirror._frame
                    if seq != last_seq and frame:
                        connection.send(frame)
                        last_seq = seq
                    try:
                        raw = connection.recv(timeout=0.015)
                    except TimeoutError:
                        continue
                    if not isinstance(raw, str) or len(raw) > 256:
                        continue
                    event = mirror._valid_event(raw)
                    if event is not None:
                        try:
                            mirror._events.put_nowait(event)
                        except queue.Full:
                            pass
            except ConnectionClosed:
                pass
            finally:
                with mirror._clients_lock:
                    mirror._client_count -= 1
                    if mirror._client_count == 0:
                        mirror._connected.clear()

        self._server = serve(handler, '127.0.0.1', self._requested_port,
                             process_request=process_request, compression=None,
                             server_header=None, max_size=256)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._server is not None:
            self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None

    def set_frame(self, frame: bytes) -> None:
        with self._frame_lock:
            self._frame = frame
            self._frame_seq += 1

    def next_event(self) -> tuple[str, float, float] | None:
        try:
            return self._events.get_nowait()
        except queue.Empty:
            return None

    @staticmethod
    def _challenge_clip(page) -> dict:
        """First find a compact iframe; otherwise sample the center of the page."""
        viewport = page.viewport_size or {'width': 1280, 'height': 720}
        width, height = viewport['width'], viewport['height']
        try:
            frames = page.locator('iframe')
            for index in range(frames.count()):
                box = frames.nth(index).bounding_box(timeout=500)
                if box and 240 <= box['width'] <= 620 and 240 <= box['height'] <= 620:
                    x, y = max(0, int(box['x'])), max(0, int(box['y']))
                    return {'x': x, 'y': y,
                            'width': min(int(box['width']), width - x),
                            'height': min(int(box['height']), height - y)}
        except Exception:
            pass
        crop_w, crop_h = min(560, width), min(560, height)
        return {'x': (width - crop_w) // 2, 'y': (height - crop_h) // 2,
                'width': crop_w, 'height': crop_h}

    @staticmethod
    def _tighten_clip(frame: bytes, clip: dict) -> dict:
        """Find the bright CAPTCHA card on Sohu's dimmed background."""
        from PIL import Image

        image = Image.open(io.BytesIO(frame)).convert('L')
        bbox = image.point(lambda value: 255 if value >= 225 else 0).getbbox()
        if bbox is None:
            return clip
        left, top, right, bottom = bbox
        width, height = right - left, bottom - top
        if not (280 <= width <= 620 and 280 <= height <= 620 and
                abs((left + right) / 2 - image.width / 2) <= image.width * .2 and
                abs((top + bottom) / 2 - image.height / 2) <= image.height * .2):
            return clip
        return {'x': clip['x'] + left, 'y': clip['y'] + top,
                'width': width, 'height': height}

    def _capture_frame(self, page, *, tighten: bool = False) -> bytes:
        frame = page.screenshot(type='jpeg', quality=55,
                                clip=self._clip, timeout=5000)
        if tighten:
            new_clip = self._tighten_clip(frame, self._clip)
            if new_clip != self._clip:
                self._clip = new_clip
                frame = page.screenshot(type='jpeg', quality=55,
                                        clip=self._clip, timeout=5000)
        return frame

    def _process_events(self, page) -> None:
        clip = self._clip
        events = []
        for _ in range(80):
            event = self.next_event()
            if event is None:
                break
            if event[0] == 'move' and events and events[-1][0] == 'move':
                # Only the latest pointer location matters when frames lag.
                events[-1] = event
            else:
                events.append(event)
        for event in events:
            kind, x, y = event
            page.mouse.move(clip['x'] + x * clip['width'],
                            clip['y'] + y * clip['height'])
            if kind == 'down':
                self._dragging = True
                page.mouse.down()
            elif kind == 'up':
                self._dragging = False
                page.mouse.up()

    def wait_for_sms_send(self, page, *, on_ready=None, timeout_s: int = 120):
        """Stream the CAPTCHA and relay input until Sohu accepts the SMS send."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from mulpubcli.http import HTTPFailure

        pending = []

        def on_response(response):
            if '/account/cv/send-sms-v2' in response.url:
                pending.append(response)

        self._clip = self._challenge_clip(page)
        page.on('response', on_response)
        try:
            for _ in range(40):
                if self._cancel_event is not None and self._cancel_event.is_set():
                    raise HTTPFailure('搜狐登录已取消', kind='verification_required')
                frame = self._capture_frame(page, tighten=True)
                if self._clip['width'] < 500 or self._clip['height'] < 500:
                    break
                page.wait_for_timeout(100)
            self.set_frame(frame)
            if on_ready is not None:
                on_ready(self.url)
            deadline = time.monotonic() + timeout_s
            next_frame = 0.0
            while time.monotonic() < deadline:
                if self._cancel_event is not None and self._cancel_event.is_set():
                    raise HTTPFailure('搜狐登录已取消', kind='verification_required')
                self._process_events(page)
                while pending:
                    response = pending.pop(0)
                    try:
                        data = response.json()
                    except ValueError:
                        continue
                    code = data.get('code') if isinstance(data, dict) else None
                    if response.ok and code in (0, 200, 2000000):
                        self._status = 'sent'
                        page.wait_for_timeout(120)
                        return response
                    if code != 9000000:
                        raise HTTPFailure(f'搜狐短信发送失败（code={code}）',
                                          kind='verification_required')
                now = time.monotonic()
                if self._connected.is_set() and now >= next_frame:
                    try:
                        self.set_frame(self._capture_frame(
                            page, tighten=self._clip['width'] >= 500))
                    except PlaywrightTimeout:
                        pass
                    next_frame = now + (0.08 if self._dragging else 0.25)
                page.wait_for_timeout(
                    5 if self._dragging else 30 if self._connected.is_set() else 100)
            raise HTTPFailure('等待页面验证和短信发送超时，登录未完成',
                              kind='verification_required')
        finally:
            page.remove_listener('response', on_response)
