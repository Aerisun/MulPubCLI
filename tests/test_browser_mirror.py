"""The temporary browser mirror keeps control on localhost and forwards pointer input."""
import importlib
import io
import json
import time
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import urlopen

from PIL import Image, ImageDraw
from websockets.sync.client import connect


class BrowserMirrorTests(unittest.TestCase):
    def _mirror(self):
        try:
            return importlib.import_module('mulpubcli.browser_mirror').BrowserMirror
        except ModuleNotFoundError:
            self.fail('browser mirror is missing')

    def test_local_page_requires_token_and_forwards_pointer_events(self):
        BrowserMirror = self._mirror()
        with BrowserMirror() as mirror:
            self.assertEqual(mirror._server.socket.getsockname()[0], '127.0.0.1')
            with urlopen(mirror.url, timeout=2) as response:
                self.assertIn(b'pointerdown', response.read())
            mirror.set_frame(b'jpeg-frame')
            with connect(mirror.url.replace('http://', 'ws://') + 'ws') as socket:
                self.assertEqual(json.loads(socket.recv(timeout=2))['status'], 'waiting')
                self.assertEqual(socket.recv(timeout=2), b'jpeg-frame')
                socket.send(json.dumps({'type': 'down', 'x': 0.2, 'y': 0.4}))
                deadline = time.monotonic() + 2
                while mirror._events.empty() and time.monotonic() < deadline:
                    time.sleep(.01)
            self.assertEqual(mirror.next_event(), ('down', 0.2, 0.4))
            with self.assertRaises(HTTPError) as denied:
                urlopen('http://127.0.0.1:' + str(mirror.port) + '/ws', timeout=2)
            self.assertEqual(denied.exception.code, 404)

    def test_invalid_pointer_event_cannot_reach_browser(self):
        BrowserMirror = self._mirror()
        with BrowserMirror() as mirror:
            with connect(mirror.url.replace('http://', 'ws://') + 'ws') as socket:
                socket.send(json.dumps({'type': 'eval', 'x': 0.5, 'y': 0.5}))
                time.sleep(.05)
            self.assertIsNone(mirror.next_event())

    def test_screenshot_area_is_limited_to_small_challenge_window(self):
        BrowserMirror = self._mirror()
        page = Mock()
        page.viewport_size = {'width': 1280, 'height': 720}
        page.locator.return_value.count.return_value = 0
        clip = BrowserMirror._challenge_clip(page)
        self.assertLessEqual(clip['width'], 560)
        self.assertLessEqual(clip['height'], 560)
        self.assertGreater(clip['x'], 0)

    def test_bright_challenge_card_is_cropped_to_its_exact_border(self):
        BrowserMirror = self._mirror()
        image = Image.new('RGB', (560, 560), '#888888')
        ImageDraw.Draw(image).rectangle((99, 100, 458, 459), fill='white')
        output = io.BytesIO()
        image.save(output, format='JPEG', quality=80)
        clip = {'x': 360, 'y': 80, 'width': 560, 'height': 560}
        cropped = BrowserMirror._tighten_clip(output.getvalue(), clip)
        self.assertEqual(cropped, {'x': 459, 'y': 180, 'width': 360, 'height': 360})

    def test_loading_indicator_does_not_lock_in_the_large_crop(self):
        BrowserMirror = self._mirror()
        def encoded_card(size):
            image = Image.new('RGB', (560, 560), '#888888')
            left = (560 - size) // 2
            ImageDraw.Draw(image).rectangle(
                (left, left, left + size - 1, left + size - 1), fill='white')
            output = io.BytesIO()
            image.save(output, format='JPEG', quality=80)
            return output.getvalue()

        mirror = BrowserMirror()
        mirror._clip = {'x': 360, 'y': 80, 'width': 560, 'height': 560}
        page = Mock()
        page.screenshot.side_effect = [encoded_card(128), encoded_card(360), b'tight']
        mirror._capture_frame(page, tighten=True)
        self.assertEqual(mirror._clip['width'], 560)
        self.assertEqual(mirror._capture_frame(page, tighten=True), b'tight')
        self.assertEqual(mirror._clip, {'x': 460, 'y': 180, 'width': 360, 'height': 360})

    def test_drag_coordinates_map_back_into_original_browser_page(self):
        BrowserMirror = self._mirror()
        mirror = BrowserMirror()
        mirror._clip = {'x': 100, 'y': 50, 'width': 400, 'height': 300}
        for event in (('down', 0.1, 0.5), ('move', 0.8, 0.5), ('up', 0.8, 0.5)):
            mirror._events.put_nowait(event)
        page = Mock()
        mirror._process_events(page)
        self.assertEqual([call.args for call in page.mouse.move.call_args_list],
                         [(140.0, 200.0), (420.0, 200.0), (420.0, 200.0)])
        page.mouse.down.assert_called_once()
        page.mouse.up.assert_called_once()

    def test_stale_drag_moves_do_not_delay_latest_pointer_position(self):
        BrowserMirror = self._mirror()
        mirror = BrowserMirror()
        mirror._clip = {'x': 0, 'y': 0, 'width': 100, 'height': 100}
        for x in (0.1, 0.2, 0.3, 0.4):
            mirror._events.put_nowait(('move', x, 0.5))
        page = Mock()
        mirror._process_events(page)
        self.assertTrue(mirror._events.empty())
        page.mouse.move.assert_called_once_with(40.0, 50.0)


if __name__ == '__main__':
    unittest.main()
