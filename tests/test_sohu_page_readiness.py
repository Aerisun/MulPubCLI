"""Exercise Sohu landing-page readiness in Chromium, including the real redirect route."""

from unittest.mock import Mock, patch

import pytest
from playwright.sync_api import Error, TimeoutError, sync_playwright

from mulpubcli.browser import LoginResult
from mulpubcli.http import HTTPFailure
from mulpubcli.platforms.sohu.login import SohuLogin


@pytest.fixture(scope="module")
def chromium():
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        except Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Chromium is not installed")
            raise
        yield browser
        browser.close()


@pytest.mark.parametrize(
    ("url", "html", "accepted"),
    [
        (
            "https://mp.sohu.com/mpfe/v4/contentManagement/first/page",
            "<body>   </body>",
            False,
        ),
        (
            "https://mp.sohu.com/mpfe/v4/contentManagement/first/page",
            "<nav>内容管理</nav><main>作品列表</main>",
            True,
        ),
        (
            "https://mp.sohu.com/mpfe/v4/contentManagement/news/addarticle",
            '<input placeholder="请输入标题">',
            True,
        ),
        (
            "https://mp.sohu.com/mpfe/v4/clientAuth",
            '<input placeholder="请输入短信验证码">',
            True,
        ),
        (
            "https://mp.sohu.com/mpfe/v4/contentManagement/unknown",
            "<main>未知页面</main>",
            False,
        ),
        ("https://mp.sohu.com/", "<main>公开首页</main>", False),
        (
            "https://example.com/mpfe/v4/contentManagement/first/page",
            "<main>内容管理</main>",
            False,
        ),
    ],
)
def test_landing_page_classification(chromium, monkeypatch, url, html, accepted):
    page = chromium.new_page()
    try:
        page.route(
            "**/*",
            lambda route: route.fulfill(
                body=html, content_type="text/html; charset=utf-8"
            ),
        )
        page.goto(url)
        wait = page.wait_for_function
        monkeypatch.setattr(
            page,
            "wait_for_function",
            lambda expression, **kwargs: wait(expression, timeout=250),
        )
        if accepted:
            SohuLogin._wait_for_editor_or_auth(page)
        else:
            with pytest.raises(TimeoutError):
                SohuLogin._wait_for_editor_or_auth(page)
    finally:
        page.close()


def test_list_landing_does_not_bypass_account_verification(tmp_path):
    target = tmp_path / "sohu.json"
    target.write_text("original", encoding="utf-8")
    login = SohuLogin("synthetic", "synthetic", target, account_id="expected")

    def browser_login(_url, perform, **_kwargs):
        page = Mock()
        page.context.cookies.return_value = [{"name": "mp-cv", "value": "old"}]
        page.url = "https://mp.sohu.com/mpfe/v4/contentManagement/first/page"
        page.evaluate.return_value = "作品列表"
        with patch.object(
            login,
            "_export",
            side_effect=lambda _: login.destination.write_text("unverified"),
        ):
            assert perform(page, None, None) is False
        return LoginResult(status="ok")

    with (
        patch("mulpubcli.browser.PlaywrightLoginer") as browser,
        patch(
            "mulpubcli.platforms.sohu.client.SohuWeb.load",
            side_effect=HTTPFailure("expired", kind="authentication_required"),
        ),
    ):
        browser.return_value.login.side_effect = browser_login
        with pytest.raises(HTTPFailure):
            login.run()
    assert target.read_text() == "original"
    assert list(tmp_path.glob(".sohu-login-*")) == []


def test_late_device_redirect_is_completed_in_the_same_browser(chromium, tmp_path):
    """A visible sidebar precedes Sohu's asynchronous device authorization guard."""
    login = SohuLogin("synthetic", "synthetic", tmp_path / "sohu.json")
    context = chromium.new_context()
    context.add_cookies(
        [{"name": "mp-cv", "value": "old", "domain": "mp.sohu.com", "path": "/"}]
    )
    page = context.new_page()
    client = Mock()
    client.account.side_effect = [
        HTTPFailure("device", kind="verification_required", code=1300),
        {"id": "123", "name": "Synthetic"},
    ]
    authorized = False
    page.route(
        "**/*",
        lambda route: route.fulfill(
            content_type="text/html; charset=utf-8",
            body='<input placeholder="请输入标题">'
            if authorized
            else """<nav>内容管理</nav><main>作品列表</main><script>
        history.replaceState({}, '', '/mpfe/v4/contentManagement/first/page');
        sessionStorage.setItem('dv-id', 'device');
        localStorage.setItem('preview-dv-id', 'device');
        localStorage.setItem('preview-sp-cm', 'authorized-device');
        setTimeout(() => {
            history.replaceState({}, '', '/mpfe/v4/clientAuth');
            document.body.innerHTML = '<input placeholder="请输入短信验证码">短信验证';
        }, 1000);
        </script>""",
        ),
    )

    def completed(current):
        nonlocal authorized
        authorized = True
        assert current.url.endswith("/clientAuth")
        assert current.locator('input[placeholder="请输入短信验证码"]').count() == 1
        current.evaluate(
            "history.replaceState({}, '', '/mpfe/v4/contentManagement/first/page')"
        )
        return True

    try:
        page.goto("https://mp.sohu.com/mpfe/v4/contentManagement/first/page")
        # goto inside _perform still uses the controlled route. No platform writes.
        with (
            patch.object(SohuLogin, "_solve_sms_code", side_effect=completed) as sms,
            patch("mulpubcli.platforms.sohu.client.SohuWeb.load", return_value=client),
        ):
            assert login._perform(page, None, None) is False
        sms.assert_called_once_with(page)
        assert client.account.call_count == 2
    finally:
        context.close()


def test_restored_preview_waits_for_the_current_browser_device(chromium, tmp_path):
    import json

    login = SohuLogin("synthetic", "synthetic", tmp_path / "sohu.json")
    context = chromium.new_context()
    context.add_cookies(
        [{"name": "mp-cv", "value": "authorized", "domain": "mp.sohu.com", "path": "/"}]
    )
    page = context.new_page()
    page.route(
        "**/*",
        lambda route: route.fulfill(
            content_type="text/html; charset=utf-8",
            body="""<nav>内容管理</nav><main>作品列表</main><script>
        history.replaceState({}, '', '/mpfe/v4/contentManagement/first/page');
        localStorage.setItem('preview-dv-id', 'old-device');
        localStorage.setItem('preview-sp-cm', 'authorized');
        sessionStorage.removeItem('dv-id');
        setTimeout(() => {
            sessionStorage.setItem('dv-id', 'current-device');
            localStorage.setItem('preview-dv-id', 'current-device');
        }, 300);
        </script>""",
        ),
    )
    client = Mock()

    def account():
        exported = json.loads(login.destination.read_text())
        assert exported["dv_id"] == "current-device"
        return {"id": "123", "name": "Synthetic"}

    client.account.side_effect = account
    try:
        page.goto("https://mp.sohu.com/mpfe/v4/contentManagement/first/page")
        with (
            patch.object(login, "_solve_sms_code") as sms,
            patch("mulpubcli.platforms.sohu.client.SohuWeb.load", return_value=client),
        ):
            assert login._perform(page, None, None) is False
        sms.assert_not_called()
    finally:
        context.close()
