"""The CLI exposes the same article identity after every platform submission."""

import json
from types import SimpleNamespace

import pytest

from mulpubcli import __main__ as cli
from mulpubcli.core import Article, PublishResult
from mulpubcli.ledger import ResultLedger
from mulpubcli.storage import StorageLayout


@pytest.mark.parametrize("platform,remote_id", [
    ("toutiao", "123456789"),
    ("netease", "L8DBTPLM0556PYDT"),
])
def test_publish_reports_saved_article_identity(
        tmp_path, monkeypatch, capsys, platform, remote_id):
    (tmp_path / "cover.jpg").write_bytes(b"cover")
    source = tmp_path / "article.md"
    source.write_text("# 测试标题\n<!-- cover: cover.jpg -->\n正文。", encoding="utf-8")
    article = Article.load(source)
    store = StorageLayout(tmp_path)

    def submit(_platform, _article, _store, **_kwargs):
        ResultLedger(store.results_dir).checkpoint(platform, article, "submitted", remote_id)
        return PublishResult("pending", "待核验", platform=platform)

    monkeypatch.setattr(cli, "_do_publish", submit)
    args = SimpleNamespace(platform=platform, article=str(source), force=False, proxy=None)
    assert cli._cmd_publish(args, store) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["platform"] == platform
    assert output["id"] == remote_id
    assert output["title"] == article.title
    assert output["status"] == "pending"
    assert "url" in output
    assert "verification" in output


def test_publish_validation_and_duplicate_results_keep_common_fields(tmp_path, capsys):
    (tmp_path / "cover.jpg").write_bytes(b"cover")
    source = tmp_path / "article.md"
    source.write_text("# 标题\n<!-- cover: cover.jpg -->\n正文。", encoding="utf-8")
    store = StorageLayout(tmp_path)
    article = Article.load(source)
    args = SimpleNamespace(platform="zhihu", article=str(source), force=False, proxy=None)
    assert ResultLedger(store.results_dir).reserve("zhihu", article)
    ResultLedger(store.results_dir).checkpoint("zhihu", article, "submitted", "123456")

    assert cli._cmd_publish(args, store) == 1
    skipped = json.loads(capsys.readouterr().out)
    assert skipped["status"] == "skipped"
    assert skipped["id"] == "123456"
    assert skipped["title"] == article.title
    assert "id" in skipped and "verification" in skipped and "url" in skipped

    invalid = tmp_path / "invalid.md"
    invalid.write_text("# 标题\n正文，没有封面。", encoding="utf-8")
    args.article = str(invalid)
    assert cli._cmd_publish(args, store) == 2
    failed = json.loads(capsys.readouterr().out)
    assert failed["status"] == "failed"
    assert "id" in failed and "title" in failed and "verification" in failed and "url" in failed
