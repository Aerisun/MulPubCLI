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
    args = SimpleNamespace(platform=platform, article=str(source), proxy=None)
    assert cli._cmd_publish(args, store) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["platform"] == platform
    assert output["id"] == remote_id
    assert output["title"] == article.title
    assert output["status"] == "pending"
    assert "url" in output
    assert "verification" in output


def test_publish_parser_does_not_accept_force_option(capsys):
    with pytest.raises(SystemExit) as raised:
        cli._build_parser().parse_args([
            "publish", "zhihu", "--article", "article.md", "--force"])
    assert raised.value.code == 2
    assert "unrecognized arguments: --force" in capsys.readouterr().err


def test_publish_resubmits_existing_article_and_keeps_common_fields(tmp_path, monkeypatch, capsys):
    (tmp_path / "cover.jpg").write_bytes(b"cover")
    source = tmp_path / "article.md"
    source.write_text("# 标题\n<!-- cover: cover.jpg -->\n正文。", encoding="utf-8")
    store = StorageLayout(tmp_path)
    article = Article.load(source)
    ledger = ResultLedger(store.results_dir)
    ledger.checkpoint("zhihu", article, "submitted", "prior-id")
    ledger.save("zhihu", article, PublishResult(
        "published", "之前已发布", "https://zhuanlan.zhihu.com/p/prior-id",
        "zhihu", "published"))
    args = SimpleNamespace(platform="zhihu", article=str(source), proxy=None)
    calls = []

    def submit(platform, submitted_article, submitted_store, **_kwargs):
        calls.append(platform)
        ResultLedger(submitted_store.results_dir).checkpoint(
            platform, submitted_article, "submitted", "123456")
        return PublishResult("pending", "等待核验", platform=platform)

    monkeypatch.setattr(cli, "_do_publish", submit)

    assert cli._cmd_publish(args, store) == 1
    submitted = json.loads(capsys.readouterr().out)
    assert calls == ["zhihu"]
    assert submitted["status"] == "pending"
    assert submitted["id"] == "123456"
    assert submitted["title"] == article.title
    assert "id" in submitted and "verification" in submitted and "url" in submitted

    invalid = tmp_path / "invalid.md"
    invalid.write_text("# 标题\n正文，没有封面。", encoding="utf-8")
    args.article = str(invalid)
    assert cli._cmd_publish(args, store) == 2
    failed = json.loads(capsys.readouterr().out)
    assert failed["status"] == "failed"
    assert "id" in failed and "title" in failed and "verification" in failed and "url" in failed
