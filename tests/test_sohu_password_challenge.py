"""Password CAPTCHA must stay in the original page until a person completes it."""

import threading
from unittest.mock import MagicMock, Mock, patch

import pytest

from mulpubcli.http import HTTPFailure
from mulpubcli.platforms.sohu.login import EDITOR_URL, LOGIN_URL, SohuLogin


def test_editor_timeout_logs_only_safe_page_classification(caplog):
    from playwright.sync_api import TimeoutError

    page = Mock()
    page.url = "https://mp.sohu.com/mpfe/v4/login?token=opaque-secret"
    page.wait_for_function.side_effect = TimeoutError("opaque-secret")
    page.evaluate.return_value = {
        "login_form": True,
        "sms_input": False,
        "editor": False,
    }
    with pytest.raises(TimeoutError):
        SohuLogin._wait_for_editor_or_auth(page)
    assert "mp.sohu.com/mpfe/v4/login" in caplog.text
    assert "login_form" in caplog.text
    assert "opaque-secret" not in caplog.text


def password_page(status, *, path="/i/login"):
    page = Mock()
    page.url = LOGIN_URL
    page.context.cookies.return_value = []
    page.evaluate.return_value = ""
    page.locator.return_value.first.is_visible.return_value = False
    listeners = {}
    page.on.side_effect = lambda event, callback: listeners.update({event: callback})

    def respond(code, *, path=path):
        response = Mock(url="https://v4.passport.sohu.com" + path, ok=True)
        response.json.return_value = {"status": code, "message": "opaque-secret"}
        if "response" in listeners:
            listeners["response"](response)

    page.locator.return_value.first.click.side_effect = lambda **_kwargs: respond(
        status
    )
    page.goto.side_effect = lambda url, **_kwargs: setattr(page, "url", url)
    return page, respond, listeners


@pytest.mark.parametrize("status", [465, "465", 419, 420, 42001])
@pytest.mark.parametrize(
    "path", ["/i/login", "/i/login/999801", "/i/login/require/captcha/999801"]
)
def test_password_challenge_opens_mirror_before_leaving_login(status, path, tmp_path):
    callback = Mock()
    login = SohuLogin(
        "synthetic-account",
        "synthetic-password",
        tmp_path / "sohu.json",
        on_challenge=callback,
    )
    page, respond, listeners = password_page(status, path=path)
    mirror = MagicMock()
    mirror.__enter__.return_value = mirror

    def complete(original, *, is_complete, on_ready, **_kwargs):
        assert original is page
        assert page.url == LOGIN_URL
        page.goto.assert_not_called()
        assert not is_complete()
        on_ready(mirror.url)
        respond(200)
        page.url = "https://v4.passport.sohu.com/fe/center"
        assert is_complete()
        return True

    mirror.wait_for_verification.side_effect = complete
    with (
        patch("mulpubcli.browser_mirror.BrowserMirror", return_value=mirror),
        patch.object(login, "_export") as export,
        patch.object(login, "_verify_device_session"),
    ):
        assert login._perform(page, None, None) is False
    mirror.wait_for_verification.assert_called_once()
    callback.assert_called_once_with(mirror.url)
    page.goto.assert_called_once_with(
        EDITOR_URL, timeout=50000, wait_until="domcontentloaded"
    )
    page.remove_listener.assert_called_once_with("response", listeners["response"])
    export.assert_called_once_with(page)


def test_successful_password_login_does_not_open_challenge(tmp_path):
    login = SohuLogin("synthetic-account", "synthetic-password", tmp_path / "sohu.json")
    page, _respond, _listeners = password_page(200)
    click = page.locator.return_value.first.click.side_effect

    def accepted(**kwargs):
        click(**kwargs)
        page.url = "https://v4.passport.sohu.com/fe/center"

    page.locator.return_value.first.click.side_effect = accepted
    with (
        patch("mulpubcli.browser_mirror.BrowserMirror") as mirror,
        patch.object(login, "_export") as export,
        patch.object(login, "_verify_device_session"),
    ):
        login._perform(page, None, None)
    mirror.assert_not_called()
    export.assert_called_once_with(page)


def test_accepted_password_response_continues_without_page_redirect(tmp_path):
    login = SohuLogin("synthetic-account", "synthetic-password", tmp_path / "sohu.json")
    page, _respond, _listeners = password_page(200, path="/i/login/999801")
    with (
        patch("mulpubcli.platforms.sohu.login.time.monotonic", side_effect=[0, 0, 13]),
        patch.object(login, "_export") as export,
        patch.object(login, "_verify_device_session"),
    ):
        login._perform(page, None, None)
    page.goto.assert_called_once_with(
        EDITOR_URL, timeout=50000, wait_until="domcontentloaded"
    )
    export.assert_called_once_with(page)


def test_completed_challenge_notifies_caller_before_entering_editor(tmp_path):
    completed = Mock()
    login = SohuLogin(
        "synthetic-account",
        "synthetic-password",
        tmp_path / "sohu.json",
        on_challenge_complete=completed,
    )
    page, respond, _listeners = password_page(465, path="/i/login/999801")
    mirror = MagicMock()
    mirror.__enter__.return_value = mirror

    def verified(_page, *, is_complete, **_kwargs):
        respond(200)
        assert page.url == LOGIN_URL
        assert is_complete()

    def enter_editor(url, **_kwargs):
        completed.assert_called_once_with()
        page.url = url

    mirror.wait_for_verification.side_effect = verified
    page.goto.side_effect = enter_editor
    with (
        patch("mulpubcli.browser_mirror.BrowserMirror", return_value=mirror),
        patch.object(login, "_export"),
        patch.object(login, "_verify_device_session"),
    ):
        login._perform(page, None, None)


def test_failed_http_response_cannot_complete_password_login(tmp_path):
    login = SohuLogin("synthetic-account", "synthetic-password", tmp_path / "sohu.json")
    page, _respond, listeners = password_page(200)
    response = Mock(
        url="https://v4.passport.sohu.com/i/login/999801", ok=False, status=503
    )
    response.json.return_value = {"status": 200}
    page.locator.return_value.first.click.side_effect = lambda **_kwargs: listeners[
        "response"
    ](response)
    with patch.object(login, "_export") as export:
        with pytest.raises(HTTPFailure) as failure:
            login._perform(page, None, None)
    assert failure.value.status_code == 503
    page.goto.assert_not_called()
    export.assert_not_called()


@pytest.mark.parametrize("status", [404, 430, 453])
@pytest.mark.parametrize("path", ["/i/login", "/i/login/999801"])
def test_rejected_password_never_navigates_to_editor(status, path, tmp_path):
    login = SohuLogin("synthetic-account", "synthetic-password", tmp_path / "sohu.json")
    page, _respond, listeners = password_page(status, path=path)
    with patch.object(login, "_export") as export:
        with pytest.raises(HTTPFailure) as failure:
            login._perform(page, None, None)
    assert failure.value.kind == "authentication_required"
    assert failure.value.code == status
    assert "opaque-secret" not in str(failure.value)
    page.goto.assert_not_called()
    page.remove_listener.assert_called_once_with("response", listeners["response"])
    export.assert_not_called()


def test_unattended_password_challenge_stops_without_mirror(tmp_path):
    login = SohuLogin(
        "synthetic-account",
        "synthetic-password",
        tmp_path / "sohu.json",
        allow_human=False,
    )
    page, _respond, _listeners = password_page(465)
    with (
        patch("mulpubcli.browser_mirror.BrowserMirror") as mirror,
        patch.object(login, "_export") as export,
        patch.object(login, "_verify_device_session"),
    ):
        with pytest.raises(HTTPFailure) as failure:
            login._perform(page, None, None)
    assert failure.value.kind == "verification_required"
    page.goto.assert_not_called()
    mirror.assert_not_called()
    export.assert_not_called()


def test_timed_out_password_challenge_preserves_original_page(tmp_path):
    completed = Mock()
    login = SohuLogin(
        "synthetic-account",
        "synthetic-password",
        tmp_path / "sohu.json",
        on_challenge_complete=completed,
    )
    page, _respond, listeners = password_page(465)
    mirror = MagicMock()
    mirror.__enter__.return_value = mirror
    mirror.wait_for_verification.side_effect = HTTPFailure(
        "等待页面验证超时", kind="verification_required"
    )
    with (
        patch("mulpubcli.browser_mirror.BrowserMirror", return_value=mirror),
        patch.object(login, "_export") as export,
        patch.object(login, "_verify_device_session"),
    ):
        with pytest.raises(HTTPFailure, match="超时"):
            login._perform(page, None, None)
    page.goto.assert_not_called()
    page.remove_listener.assert_called_once_with("response", listeners["response"])
    export.assert_not_called()
    completed.assert_not_called()


def test_cancelled_password_login_never_exports_credentials(tmp_path):
    cancelled = threading.Event()
    cancelled.set()
    login = SohuLogin(
        "synthetic-account",
        "synthetic-password",
        tmp_path / "sohu.json",
        cancel_event=cancelled,
    )
    page, _respond, _listeners = password_page(465)
    with patch.object(login, "_export") as export:
        with pytest.raises(HTTPFailure, match="取消"):
            login._perform(page, None, None)
    page.goto.assert_not_called()
    export.assert_not_called()


def test_password_challenge_rejection_after_human_input_does_not_export(tmp_path):
    login = SohuLogin("synthetic-account", "synthetic-password", tmp_path / "sohu.json")
    page, respond, _listeners = password_page(465)
    mirror = MagicMock()
    mirror.__enter__.return_value = mirror

    def rejected(_page, *, is_complete, **_kwargs):
        respond(453)
        is_complete()

    mirror.wait_for_verification.side_effect = rejected
    with (
        patch("mulpubcli.browser_mirror.BrowserMirror", return_value=mirror),
        patch.object(login, "_export") as export,
        patch.object(login, "_verify_device_session"),
    ):
        with pytest.raises(HTTPFailure) as failure:
            login._perform(page, None, None)
    assert failure.value.kind == "authentication_required"
    page.goto.assert_not_called()
    export.assert_not_called()


def test_visible_browser_waits_in_original_login_page(tmp_path):
    login = SohuLogin("synthetic-account", "synthetic-password", tmp_path / "sohu.json")
    login._headless = False
    page, respond, _listeners = password_page(465)

    def complete(_milliseconds):
        page.goto.assert_not_called()
        respond(200)
        page.url = "https://v4.passport.sohu.com/fe/center"

    page.wait_for_timeout.side_effect = complete
    with (
        patch("mulpubcli.browser_mirror.BrowserMirror") as mirror,
        patch.object(login, "_export") as export,
        patch.object(login, "_verify_device_session"),
    ):
        login._perform(page, None, None)
    mirror.assert_not_called()
    export.assert_called_once_with(page)


def test_missing_login_response_stops_before_editor(tmp_path):
    login = SohuLogin("synthetic-account", "synthetic-password", tmp_path / "sohu.json")
    page, _respond, listeners = password_page(200)
    page.locator.return_value.first.click.side_effect = None
    with (
        patch("mulpubcli.platforms.sohu.login.time.monotonic", side_effect=[0, 0, 13]),
        patch.object(login, "_export") as export,
        patch.object(login, "_verify_device_session"),
    ):
        with pytest.raises(HTTPFailure, match="超时"):
            login._perform(page, None, None)
    page.goto.assert_not_called()
    page.remove_listener.assert_called_once_with("response", listeners["response"])
    export.assert_not_called()
