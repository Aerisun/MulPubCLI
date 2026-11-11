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
        result = login.run()
    assert result["status"] == "failed"
    assert target.read_text() == "original"
    assert list(tmp_path.glob(".sohu-login-*")) == []
