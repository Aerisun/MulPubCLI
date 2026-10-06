"""The public list tracks CLI submissions, not an account's whole remote feed."""

import io
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from mulpubcli import __main__ as cli
from mulpubcli.core import Article, PublishResult
from mulpubcli.ledger import ResultLedger
from mulpubcli.storage import StorageLayout


def _record(store: StorageLayout, platform: str, key: str, **fields) -> Path:
    path = store.results_dir / f"{platform}-{key}.json"
    path.write_text(json.dumps({"platform": platform, "title": key,
                                "status": "pending", "reserved_at": "2026-10-03T01:00:00+00:00",
                                **fields}), encoding="utf-8")
    return path


def test_millisecond_times_are_formatted_in_china_time():
    expected = datetime.fromtimestamp(1791132099, ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    assert cli._fmt_time(1791132099000) == expected
    assert cli._fmt_time("1791132099000") == expected
    assert cli._fmt_time("2026-10-04T05:58:55+00:00") == "2026-10-04 13:58"


def test_list_shows_only_tracked_local_articles_and_chinese_platform(tmp_path, monkeypatch, capsys):
    store = StorageLayout(tmp_path)
    _record(store, "netease", "one", remote_id="L8EH25420556PYDT", status="published")
    _record(store, "netease", "two", remote_id="L8EGM2330556PYDT", status="published", tracked=False)
    monkeypatch.setattr(cli, "_list_platform", lambda *a, **k: {
        "source": "live", "complete": True, "items": [
            {"id": "L8EH25420556PYDT", "title": "CLI文章", "status": "published",
             "published_at": "1791132099000", "url": "https://www.163.com/dy/article/L8EH25420556PYDT.html"},
            {"id": "L8EGM2330556PYDT", "title": "已取消跟踪", "status": "published"},
            {"id": "L8EFUTU00556PYDT", "title": "平台旧文章", "status": "published"},
        ]})
    assert cli._cmd_list(SimpleNamespace(platform="netease", json=True, proxy=None), store) == 0
    items = json.loads(capsys.readouterr().out)["platforms"]["netease"]["items"]
    assert len(items) == 1
    assert items[0]["title"] == "CLI文章"
    assert items[0]["published_at"].startswith("2026-")
    assert items[0]["tracking_id"] == "netease-one"
    shown = cli._list_human({"netease": {"source": "live", "items": items}})
    assert "网易号" in shown
    assert "netease" not in shown


def test_list_keeps_saved_verification_limit_when_feed_only_says_published(tmp_path, monkeypatch, capsys):
    store = StorageLayout(tmp_path)
    note_id = "a" * 24
    direct = f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=share"
    _record(store, "xiaohongshu", "one", remote_id=note_id, status="published", url=direct,
            last_verification={"status": "published", "verification": "published",
                               "message": "图片像素暂未核对"})
    monkeypatch.setattr(cli, "_list_platform", lambda *a, **k: {
        "source": "live", "complete": True, "items": [{
            "id": note_id, "title": "测试", "status": "published", "url": direct, "check": None}]})
    assert cli._cmd_list(SimpleNamespace(platform="xiaohongshu", json=True, proxy=None), store) == 0
    item = json.loads(capsys.readouterr().out)["platforms"]["xiaohongshu"]["items"][0]
    assert item["status"] == "published"
    assert item["verification"] == "published"
    assert item["check"] == "图片像素暂未核对"


def test_platform_specific_list_does_not_print_other_platforms_as_errors():
    shown = cli._list_human({"netease": {"source": "ledger", "items": [{
        "id": "L8EH25420556PYDT", "title": "测试", "status": "published",
        "url": "https://www.163.com/dy/article/L8EH25420556PYDT.html"}]}})
    assert "网易号" in shown
    assert "读取失败" not in shown
    assert "小红书" not in shown


def test_local_record_without_remote_id_gets_a_deletable_tracking_id(tmp_path):
    store = StorageLayout(tmp_path)
    _record(store, "toutiao", "pending")
    item = cli._ledger_items("toutiao", store)[0]
    assert item["id"] is None
    assert item["tracking_id"] == "toutiao-pending"
    assert "toutiao-pending" in cli._list_human({"toutiao": {"source": "ledger", "items": [item]}})


def test_missing_old_title_comes_from_same_article_fingerprint(tmp_path):
    store = StorageLayout(tmp_path)
    key = "59399ec9c0373dce1a6b"
    _record(store, "netease", key, title="那些在时光中沉淀的温柔")
    _record(store, "zhihu", key, title=None, remote_id="123456", status="published")
    item = cli._ledger_items("zhihu", store)[0]
    assert item["title"] == "那些在时光中沉淀的温柔"
    assert cli._zhihu_ledger_items(store)[0]["title"] == "那些在时光中沉淀的温柔"


def test_conflicting_titles_do_not_fill_legacy_record(tmp_path):
    store = StorageLayout(tmp_path)
    key = "59399ec9c0373dce1a6b"
    _record(store, "netease", key, title="标题甲")
    _record(store, "sohu", key, title="标题乙")
    _record(store, "zhihu", key, title=None, remote_id="123456", status="published")
    assert cli._ledger_items("zhihu", store)[0]["title"] == "未知"


def test_failed_attempt_without_remote_article_is_not_in_publication_list(tmp_path):
    store = StorageLayout(tmp_path)
    _record(store, "sohu", "failed", status="failed", message="投稿前被拒绝")
    assert cli._ledger_items("sohu", store) == []
    assert (store.results_dir / "sohu-failed.json").is_file()


def test_failed_remote_article_stays_in_tracking_list(tmp_path):
    store = StorageLayout(tmp_path)
    _record(store, "sohu", "rejected", status="failed", remote_id="123456")
    assert cli._ledger_items("sohu", store)[0]["status"] == "failed"


def test_zhihu_without_credentials_shows_saved_status_as_unverified(tmp_path, monkeypatch):
    store = StorageLayout(tmp_path)
    _record(store, "zhihu", "one", remote_id="123456", status="published",
            saved_at="2026-10-04T15:00:00+00:00")
    monkeypatch.setattr(cli, "_load_client", lambda *a, **k: (_ for _ in ()).throw(
        FileNotFoundError("缺少登录凭证")))
    item = cli._list_platform("zhihu", store)["items"][0]
    assert item["status"] == "published"
    assert "未在线回查" in item["check"]
    assert item["published_at"] == "2026-10-03 09:00"


def test_list_delete_requires_y_and_accepts_multiple_space_separated_ids(tmp_path, monkeypatch, capsys):
    store = StorageLayout(tmp_path)
    first = _record(store, "toutiao", "one", remote_id="111", status="published")
    second = _record(store, "netease", "two", status="draft")
    monkeypatch.setattr(cli, "_load_client", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("list-delete must not call the platform")))

    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert cli.main(["--root", str(tmp_path), "list-delete", "111", "netease-two"]) == 0
    declined = capsys.readouterr().out
    assert "111" in declined and "netease-two" in declined
    assert "标题" in declined and "发布时间" in declined and "状态" in declined
    assert "没有删除" in declined
    assert json.loads(first.read_text()).get("tracked") is not False

    monkeypatch.setattr("sys.stdin", io.StringIO("n\n"))
    assert cli.main(["--root", str(tmp_path), "list-delete", "111", "netease-two"]) == 0
    assert "没有删除" in capsys.readouterr().out
    assert json.loads(second.read_text()).get("tracked") is not False

    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    assert cli.main(["--root", str(tmp_path), "list-delete", "111", "netease-two"]) == 0
    accepted = capsys.readouterr().out
    assert "成功" in accepted
    assert json.loads(first.read_text())["tracked"] is False
    assert json.loads(second.read_text())["tracked"] is False
    assert first.exists() and second.exists()
    assert cli._ledger_items("toutiao", store) == []
    assert cli._ledger_items("netease", store) == []


def test_list_delete_rejects_unknown_id_before_modifying_any_record(tmp_path, monkeypatch, capsys):
    store = StorageLayout(tmp_path)
    path = _record(store, "toutiao", "one", remote_id="111")
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    assert cli.main(["--root", str(tmp_path), "list-delete", "111", "missing"]) != 0
    assert json.loads(path.read_text()).get("tracked") is not False
    assert "missing" in capsys.readouterr().out


def test_list_delete_rejects_ambiguous_remote_id_and_accepts_local_tracking_id(tmp_path, monkeypatch, capsys):
    store = StorageLayout(tmp_path)
    first = _record(store, "toutiao", "one", remote_id="111")
    second = _record(store, "sohu", "two", remote_id="111")
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    assert cli.main(["--root", str(tmp_path), "list-delete", "111"]) != 0
    assert "对应多条" in capsys.readouterr().out
    assert json.loads(first.read_text()).get("tracked") is not False
    assert json.loads(second.read_text()).get("tracked") is not False
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    assert cli.main(["--root", str(tmp_path), "list-delete", "toutiao-one"]) == 0
    assert json.loads(first.read_text())["tracked"] is False
    assert json.loads(second.read_text()).get("tracked") is not False


def test_untracking_hides_record_without_deleting_its_history(tmp_path):
    (tmp_path / "cover.jpg").write_bytes(b"cover")
    (tmp_path / "article.md").write_text("<!-- title: 测试标题 -->\n<!-- cover: cover.jpg -->\n正文。", encoding="utf-8")
    article = Article.load(tmp_path / "article.md")
    store = StorageLayout(tmp_path)
    ledger = ResultLedger(store.results_dir)
    path = ledger.save("zhihu", article, PublishResult(
        "published", "已发布", "https://zhuanlan.zhihu.com/p/123456", "zhihu", "published"))
    stamp = json.loads(path.read_text())["saved_at"]
    assert ledger.untrack_records([(path.stem, stamp)]) == 1
    assert cli._ledger_items("zhihu", store) == []
    assert path.exists()
    assert json.loads(path.read_text())["status"] == "published"
    ledger.save("zhihu", article, PublishResult(
        "published", "重新提交", "https://zhuanlan.zhihu.com/p/123456", "zhihu", "published"))
    assert json.loads(path.read_text())["tracked"] is True


def test_failed_repeat_keeps_previous_published_article_in_list(tmp_path, monkeypatch, capsys):
    (tmp_path / "cover.jpg").write_bytes(b"cover")
    source = tmp_path / "article.md"
    source.write_text("<!-- title: 测试标题 -->\n<!-- cover: cover.jpg -->\n正文。", encoding="utf-8")
    article = Article.load(source)
    store = StorageLayout(tmp_path)
    ledger = ResultLedger(store.results_dir)
    ledger.checkpoint("netease", article, "submitted", "L8EH25420556PYDT")
    ledger.save("netease", article, PublishResult(
        "published", "已发布", "https://www.163.com/dy/article/L8EH25420556PYDT.html",
        "netease", "published"))
    monkeypatch.setattr(cli, "_do_publish", lambda *a, **k: PublishResult(
        "failed", "登录需要人工验证", platform="netease"))
    code = cli._cmd_publish(SimpleNamespace(platform="netease", article=str(source),
                                            proxy=None), store)
    assert code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    items = cli._ledger_items("netease", store)
    assert len(items) == 1
    assert items[0]["status"] == "published"
    assert items[0]["url"] == "https://www.163.com/dy/article/L8EH25420556PYDT.html"
    records = list(store.results_dir.glob("netease-*.json"))
    assert len(records) == 2


def test_repeat_after_failed_attempt_submits_again(tmp_path, monkeypatch, capsys):
    (tmp_path / "cover.jpg").write_bytes(b"cover")
    source = tmp_path / "article.md"
    source.write_text("<!-- title: 测试标题 -->\n<!-- cover: cover.jpg -->\n正文。", encoding="utf-8")
    article = Article.load(source)
    store = StorageLayout(tmp_path)
    ledger = ResultLedger(store.results_dir)
    url = "https://www.163.com/dy/article/L8EH25420556PYDT.html"
    ledger.checkpoint("netease", article, "submitted", "L8EH25420556PYDT")
    ledger.save("netease", article, PublishResult("published", "已发布", url, "netease", "verified"))
    calls = []

    def submit(platform, submitted_article, submitted_store, **_kwargs):
        calls.append(True)
        article_id = f"L8EH25420556PYD{len(calls)}"
        ResultLedger(submitted_store.results_dir).checkpoint(
            platform, submitted_article, "submitted", article_id)
        status = "failed" if len(calls) == 1 else "pending"
        return PublishResult(status, "本次结果", platform=platform)

    monkeypatch.setattr(cli, "_do_publish", submit)
    args = SimpleNamespace(platform="netease", article=str(source), proxy=None)
    assert cli._cmd_publish(args, store) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    assert cli._cmd_publish(args, store) == 1
    repeated = json.loads(capsys.readouterr().out)
    assert calls == [True, True]
    assert repeated["status"] == "pending"
    assert repeated["id"] == "L8EH25420556PYD2"
    assert any(item["id"] == "L8EH25420556PYDT" and item["status"] == "published"
               for item in cli._ledger_items("netease", store))


def test_second_publication_tracks_both_remote_article_ids(tmp_path, monkeypatch):
    (tmp_path / "cover.jpg").write_bytes(b"cover")
    source = tmp_path / "article.md"
    source.write_text("<!-- title: 测试标题 -->\n<!-- cover: cover.jpg -->\n正文。", encoding="utf-8")
    article = Article.load(source)
    store = StorageLayout(tmp_path)
    ledger = ResultLedger(store.results_dir)
    ledger.checkpoint("zhihu", article, "submitted", "123456")
    ledger.save("zhihu", article, PublishResult(
        "published", "已发布", "https://zhuanlan.zhihu.com/p/123456", "zhihu", "published"))

    def publish(*args, **kwargs):
        ledger.checkpoint("zhihu", article, "submitted", "789012")
        return PublishResult("published", "再次发布", "https://zhuanlan.zhihu.com/p/789012",
                             "zhihu", "published")

    monkeypatch.setattr(cli, "_do_publish", publish)
    cli._cmd_publish(SimpleNamespace(platform="zhihu", article=str(source),
                                     proxy=None), store)
    assert {item["id"] for item in cli._ledger_items("zhihu", store)} == {"123456", "789012"}


def test_bulk_verify_ignores_remote_only_and_untracked_articles(tmp_path, monkeypatch):
    store = StorageLayout(tmp_path)
    _record(store, "netease", "one", remote_id="L8EH25420556PYDT", status="published")
    _record(store, "netease", "two", remote_id="L8EGM2330556PYDT", tracked=False)
    monkeypatch.setattr(cli, "_list_platform", lambda *a, **k: {
        "source": "live", "complete": True, "items": [
            {"id": "L8EH25420556PYDT", "title": "跟踪文章", "status": "published"},
            {"id": "L8EGM2330556PYDT", "title": "已取消", "status": "published"},
            {"id": "L8EFUTU00556PYDT", "title": "其他旧文", "status": "published"},
        ]})
    checked = []

    def verify(article_id, **kwargs):
        checked.append(article_id)
        from mulpubcli.core import PublishResult
        return PublishResult("published", "已核验", f"https://www.163.com/dy/article/{article_id}.html",
                             "netease", "published")

    monkeypatch.setattr(cli, "_load_client", lambda *a, **k: SimpleNamespace(verify=verify, close=lambda: None))
    result = cli._refresh_platform("netease", store, proxy=None)
    assert checked == ["L8EH25420556PYDT"]
    assert len(result["published"]) == 1
