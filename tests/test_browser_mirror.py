"""The temporary browser mirror keeps control on localhost and forwards pointer input."""
import importlib
import io
import json
import time
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import urlopen

from PIL import Image, ImageDraw
from websockets.sync.client import connect
from mulpubcli.http import HTTPFailure


class BrowserMirrorTests(unittest.TestCase):
    def test_sms_send_uses_same_mirror_and_keeps_existing_sent_status(self):
        mirror = self._mirror()()
        page = Mock()
        listeners = {}
        page.on.side_effect = lambda event, callback: listeners.update(
            {event: callback}
        )
        response = Mock(url="https://mp.sohu.com/account/cv/send-sms-v2", ok=True)
        response.json.return_value = {"code": 2000000}

        def finish(original, *, is_complete, completed_status, **_kwargs):
            self.assertIs(original, page)
            self.assertEqual(completed_status, "sent")
            listeners["response"](response)
            return is_complete()

        with patch.object(mirror, "wait_for_verification", side_effect=finish):
            self.assertIs(mirror.wait_for_sms_send(page), response)
        page.remove_listener.assert_called_once_with("response", listeners["response"])

    def test_text_and_edit_keys_reach_original_captcha_input(self):
        mirror = self._mirror()()
        mirror._clip = {"x": 100, "y": 50, "width": 360, "height": 360}
        for event in (
            {"type": "text", "text": "A7中"},
            {"type": "key", "key": "Backspace"},
            {"type": "key", "key": "Enter"},
        ):
            parsed = mirror._valid_event(json.dumps(event))
            self.assertIsNotNone(parsed)
            mirror._events.put_nowait(parsed)
        page = Mock()
        mirror._process_events(page)
        page.keyboard.insert_text.assert_called_once_with("A7中")
        self.assertEqual(
            [call.args for call in page.keyboard.press.call_args_list],
            [("Backspace",), ("Enter",)],
        )
        page.mouse.move.assert_not_called()

    def test_mirror_rejects_control_characters_and_browser_shortcuts(self):
        validate = self._mirror()._valid_event
        for event in (
            {"type": "text", "text": "code\nscript"},
            {"type": "text", "text": "x" * 65},
            {"type": "text", "text": ""},
            {"type": "key", "key": "Control+L"},
            {"type": "key", "key": "F12"},
        ):
            self.assertIsNone(validate(json.dumps(event)))

    def test_password_verification_streams_original_page_and_relays_input(self):
        mirror = self._mirror()()
        page = Mock()
        ready = Mock()
        completed = Mock(side_effect=[False, True])
        with (
            patch.object(mirror, "_capture_frame", return_value=b"captcha"),
            patch.object(
                mirror,
                "_challenge_clip",
                return_value={"x": 100, "y": 50, "width": 360, "height": 360},
            ),
            patch.object(mirror, "_process_events") as events,
            patch.object(type(mirror), "url", "http://127.0.0.1/challenge/"),
        ):
            self.assertTrue(
                mirror.wait_for_verification(
                    page, is_complete=completed, on_ready=ready
                )
            )
        ready.assert_called_once_with("http://127.0.0.1/challenge/")
        events.assert_called_once_with(page)
        self.assertEqual(mirror._frame, b"captcha")
        self.assertEqual(mirror._status, "verified")

    def test_password_verification_cancellation_stops_without_frames(self):
        cancelled = threading.Event()
        cancelled.set()
        mirror = self._mirror()(cancel_event=cancelled)
        with (
            self.assertRaisesRegex(HTTPFailure, "取消"),
            patch.object(mirror, "_capture_frame") as capture,
        ):
            mirror.wait_for_verification(Mock(), is_complete=lambda: False)
        capture.assert_not_called()

    def test_password_verification_times_out_without_reporting_success(self):
        mirror = self._mirror()()
        with self.assertRaisesRegex(HTTPFailure, "超时"):
            mirror.wait_for_verification(Mock(), is_complete=lambda: False, timeout_s=0)
        self.assertEqual(mirror._status, "waiting")

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
