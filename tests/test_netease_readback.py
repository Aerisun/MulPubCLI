"""Read-only NetEase verification when the account endpoint requires a phone check."""

from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import requests

from mulpubcli.core import content_fingerprint
from mulpubcli.http import HTTPFailure
from mulpubcli.platforms.netease.client import NeteaseWeb


ARTICLE_ID = "TEST_ARTICLE_001"
ACCOUNT_ID = "test_creator"
ACCOUNT_PATH = "/wemedia/navinfo.do"
LIST_PATH = "/wemedia/content/manage/list.do"
DETAIL_PATH = "/wemedia/article/editpage.do"


@pytest.fixture
def readback_client():
    clients = []

    def create(
        *,
        account_code=100030,
        saved_id=ACCOUNT_ID,
        detail=None,
        rows=None,
        detail_code=1,
        list_code=1,
    ):
        session = requests.Session()
        session.cookies.set("NTESwebSI", "synthetic-session", domain="mp.163.com")
        client = NeteaseWeb(session=session)
        client.wemedia_id = saved_id
        clients.append(client)
        calls = []
        post = (
            detail
            if detail is not None
            else {
                "docid": ARTICLE_ID,
                "wemediaId": ACCOUNT_ID,
                "postState": 3,
            }
        )
        items = (
            rows
            if rows is not None
            else [
                {
                    "articleId": ARTICLE_ID,
                    "contentState": 3,
                    "unrecomReason": "内容分发受限",
                    "title": "测试文章",
                }
            ]
        )

        def request(method, url, **kwargs):
            path = urlsplit(url).path
            calls.append((method, path, kwargs))
            if path == ACCOUNT_PATH:
                assert method == "GET"
                payload = {
                    "code": account_code,
                    "data": {
                        "wemediaId": ACCOUNT_ID,
                        "tname": "测试作者",
                    },
                }
            elif path == DETAIL_PATH:
                assert method == "GET"
                assert kwargs["params"] == {
                    "postId": ARTICLE_ID,
                    "wemediaId": ACCOUNT_ID,
                    "mediaId": ACCOUNT_ID,
                }
                payload = {"code": detail_code, "data": {"post": post}}
            elif path == LIST_PATH:
                assert method == "POST"
                assert kwargs["data"]["wemediaId"] == ACCOUNT_ID
                state = kwargs["data"]["contentState"]
                payload = {
                    "code": list_code,
                    "data": {
                        "list": [
                            item for item in items if item["contentState"] == state
                        ],
                    },
                }
            else:
                pytest.fail(f"Unexpected platform operation: {method} {path}")
            return SimpleNamespace(status_code=200, headers={}, json=lambda: payload)

        session.request = request
        return client, calls

    yield create
    for client in clients:
        client.close()


def test_account_phone_challenge_has_safe_diagnostics(readback_client):
    client, _ = readback_client()
    with pytest.raises(HTTPFailure) as failure:
        client.account()

    assert failure.value.kind == "verification_required"
    assert failure.value.code == 100030
    assert failure.value.status_code == 200
    assert failure.value.request_method == "GET"
    assert failure.value.request_target == "mp.163.com" + ACCOUNT_PATH


def test_phone_challenge_verifies_owned_article_and_retains_distribution_warning(
    readback_client,
):
    client, calls = readback_client()

    result = client.verify(ARTICLE_ID)

    assert result.status == "published"
    assert result.verification == "published"
    assert result.url == f"https://www.163.com/dy/article/{ARTICLE_ID}.html"
    assert "分发" in result.message and "受限" in result.message
    assert "未" in result.message
    assert [path for _, path, _ in calls].count(ACCOUNT_PATH) == 1
    assert calls[1][1] == DETAIL_PATH
    assert any(path == LIST_PATH for _, path, _ in calls)


@pytest.mark.parametrize("saved_id", ["", "not a creator", "x" * 65])
def test_phone_challenge_requires_valid_saved_identity(readback_client, saved_id):
    client, calls = readback_client(saved_id=saved_id)
    with pytest.raises(HTTPFailure) as failure:
        client.verify(ARTICLE_ID)

    assert failure.value.code == 100030
    assert [path for _, path, _ in calls] == [ACCOUNT_PATH]


@pytest.mark.parametrize("account_code", [8, 9, 70, 401, 403, 987654])
def test_only_known_account_phone_challenge_allows_readback(
    readback_client, account_code
):
    client, calls = readback_client(account_code=account_code)
    with pytest.raises(HTTPFailure) as failure:
        client.verify(ARTICLE_ID)

    assert failure.value.code == account_code
    assert [path for _, path, _ in calls] == [ACCOUNT_PATH]


def test_account_network_failure_does_not_allow_readback(readback_client):
    client, calls = readback_client()

    def fail_request(*args, **kwargs):
        raise requests.Timeout("synthetic timeout")

    client.http.session.request = fail_request
    with pytest.raises(HTTPFailure) as failure:
        client.verify(ARTICLE_ID)

    assert failure.value.kind == "network_timeout"
    assert calls == []


@pytest.mark.parametrize(
    "detail",
    [
        {},
        [],
        {"docid": "OTHER_ARTICLE", "wemediaId": ACCOUNT_ID, "postState": 3},
        {"docid": ARTICLE_ID, "wemediaId": "other_creator", "postState": 3},
        {"docid": ARTICLE_ID, "postState": 3},
    ],
)
def test_phone_challenge_does_not_accept_unowned_or_missing_detail(
    readback_client, detail
):
    client, calls = readback_client(detail=detail)
    with pytest.raises(HTTPFailure) as failure:
        client.verify(ARTICLE_ID)

    assert failure.value.kind in {"account_mismatch", "invalid_response"}
    assert failure.value.status_code == 200
    assert failure.value.request_target == "mp.163.com" + DETAIL_PATH
    assert [path for _, path, _ in calls] == [ACCOUNT_PATH, DETAIL_PATH]


def test_phone_challenge_does_not_accept_list_from_another_account(readback_client):
    client, _ = readback_client(
        rows=[
            {
                "articleId": ARTICLE_ID,
                "wemediaId": "other_creator",
                "contentState": 3,
            }
        ]
    )
    with pytest.raises(HTTPFailure) as failure:
        client.verify(ARTICLE_ID)

    assert failure.value.kind == "account_mismatch"
    assert failure.value.status_code == 200
    assert failure.value.request_target == "mp.163.com" + LIST_PATH


@pytest.mark.parametrize("endpoint", [DETAIL_PATH, LIST_PATH])
def test_phone_challenge_does_not_ignore_network_failure(readback_client, endpoint):
    client, calls = readback_client()
    request = client.http.session.request

    def fail_request(method, url, **kwargs):
        if urlsplit(url).path == endpoint:
            raise requests.Timeout("synthetic timeout")
        return request(method, url, **kwargs)

    client.http.session.request = fail_request
    with pytest.raises(HTTPFailure) as failure:
        client.verify(ARTICLE_ID)

    assert failure.value.kind == "network_timeout"
    assert [path for _, path, _ in calls].count(ACCOUNT_PATH) == 1


@pytest.mark.parametrize("endpoint", ["detail", "list"])
def test_phone_challenge_does_not_ignore_other_readback_errors(
    readback_client, endpoint
):
    client, _ = readback_client(**{f"{endpoint}_code": 401})
    with pytest.raises(HTTPFailure) as failure:
        client.verify(ARTICLE_ID)

    assert failure.value.kind == "authentication_required"
    assert failure.value.code == 401
    assert failure.value.request_target == "mp.163.com" + (
        DETAIL_PATH if endpoint == "detail" else LIST_PATH
    )


@pytest.mark.parametrize(
    ("state", "expected_status"),
    [
        (0, "draft"),
        (1, "pending"),
        (3, "published"),
        (6, "deleted"),
        (7, "deleted"),
    ],
)
def test_phone_challenge_reads_owned_detail_when_lists_do_not_contain_article(
    readback_client,
    state,
    expected_status,
):
    client, _ = readback_client(
        rows=[],
        detail={
            "docid": ARTICLE_ID,
            "wemediaId": ACCOUNT_ID,
            "postState": state,
        },
    )

    result = client.verify(ARTICLE_ID)

    assert result.status == expected_status
    assert "手机号验证" in result.message
    if state in (6, 7):
        assert result.url is None
        assert "单篇详情" in result.message


@pytest.mark.parametrize(
    ("state", "expected_status"), [(None, "pending"), (1, "pending"), (7, "deleted")]
)
def test_phone_challenge_does_not_override_detail_state_with_stale_list(
    readback_client,
    state,
    expected_status,
):
    client, _ = readback_client(
        detail={
            "docid": ARTICLE_ID,
            "wemediaId": ACCOUNT_ID,
            "postState": state,
        }
    )

    result = client.verify(ARTICLE_ID)

    assert result.status == expected_status


def test_phone_challenge_does_not_claim_public_content_verification(readback_client):
    media = "https://example.ws.126.net/fixture.jpg"
    client, _ = readback_client(
        rows=[
            {
                "articleId": ARTICLE_ID,
                "contentState": 3,
                "title": "测试文章",
                "content": f'<p>测试正文</p><img src="{media}">',
            }
        ]
    )
    proof = {**content_fingerprint("netease", "测试文章", "测试正文"), "media": [media]}

    result = client.verify(ARTICLE_ID, evidence=proof)

    assert result.status == "published"
    assert result.verification == "published"
    assert "核对通过" not in result.message


@pytest.mark.parametrize(
    "content",
    [
        "<p>测试正文</p>",
        '<img src="https://example.ws.126.net/fixture.jpg">',
        '<p>测试正文</p><img src="https://example.ws.126.net/different.jpg">',
    ],
)
def test_normal_account_does_not_claim_content_verified_without_matching_text_and_images(
    readback_client,
    content,
):
    client, _ = readback_client(
        account_code=1,
        rows=[
            {
                "articleId": ARTICLE_ID,
                "contentState": 3,
                "title": "测试文章",
                "content": content,
            }
        ],
    )
    proof = {
        **content_fingerprint("netease", "测试文章", "测试正文"),
        "media": ["https://example.ws.126.net/fixture.jpg"],
    }

    result = client.verify(ARTICLE_ID, evidence=proof)

    assert result.status == "published"
    assert result.verification == "published"
    assert "核对通过" not in result.message


def test_normal_account_preserves_verified_matching_text_and_images(readback_client):
    media = "https://example.ws.126.net/fixture.jpg"
    client, _ = readback_client(
        account_code=1,
        rows=[
            {
                "articleId": ARTICLE_ID,
                "contentState": 3,
                "title": "测试文章",
                "content": f'<p>测试正文</p><img src="{media}">',
            }
        ],
    )
    proof = {**content_fingerprint("netease", "测试文章", "测试正文"), "media": [media]}

    result = client.verify(ARTICLE_ID, evidence=proof)

    assert result.status == "published"
    assert result.verification == "verified"


def test_normal_account_still_verifies_offline_detail(readback_client):
    client, _ = readback_client(
        account_code=1,
        rows=[],
        detail={
            "docid": ARTICLE_ID,
            "wemediaId": ACCOUNT_ID,
            "postState": 7,
        },
    )

    result = client.verify(ARTICLE_ID)

    assert result.status == "deleted"
    assert result.url is None


@pytest.mark.parametrize("network", ["direct", "environment"])
def test_network_mode_does_not_inject_local_proxy(network):
    with requests.Session() as session:
        client = NeteaseWeb(session=session, network=network)
        assert client.http.session is session
        assert session.proxies == {}
        assert session.trust_env is (network == "environment")


def test_explicit_session_proxy_is_preserved():
    with requests.Session() as session:
        session.proxies = {"https": "http://proxy.example.test:8080"}
        NeteaseWeb(session=session)
        assert session.proxies == {"https": "http://proxy.example.test:8080"}
