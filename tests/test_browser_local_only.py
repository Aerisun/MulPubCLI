"""浏览器登录不开放远程调试端口，人工步骤仅使用本地通知。"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from mulpubcli.browser import PlaywrightLoginer


class BrowserLocalOnlyTests(unittest.TestCase):
    def test_launch_has_no_remote_debugging_arguments(self):
        with TemporaryDirectory() as root:
            login = PlaywrightLoginer(storage_state_dir=Path(root))
            login._setup_profile()
            playwright = Mock()
            engine = playwright.start.return_value
            with patch('mulpubcli.browser._playwright', return_value=lambda: playwright):
                login._launch(headless=True)
            try:
                kwargs = engine.chromium.launch_persistent_context.call_args.kwargs
                self.assertFalse(any('remote-debugging' in arg or 'remote-allow-origins' in arg
                                     for arg in kwargs['args']))
            finally:
                login.close()

    def test_human_notification_does_not_receive_debug_address(self):
        with TemporaryDirectory() as root:
            login = PlaywrightLoginer(storage_state_dir=Path(root))
            page = Mock()
            login._context = Mock()
            login._context.new_page.return_value = page
            notified = Mock()
            with patch.object(login, '_setup_profile'), patch.object(login, '_launch'):
                result = login.login('https://example.com', lambda *args: True,
                                     on_human_needed=notified)
            self.assertEqual(result.status, 'need_human')
            notified.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
