"""Exporter CLI: argument parsing and the guards that protect the archive."""

from __future__ import annotations

import pytest

from bitbucket_export.cli import main, parse_repository


def test_parse_repository_accepts_a_slug():
    assert parse_repository("workspace/repo") == ("workspace", "repo")


def test_parse_repository_accepts_a_full_url():
    assert parse_repository("https://bitbucket.org/workspace/repo/issues") == ("workspace", "repo")


def test_parse_repository_rejects_a_bare_name():
    with pytest.raises(ValueError, match="workspace/repo"):
        parse_repository("just-a-repo")


@pytest.mark.parametrize("flags", [["--issue-id", "1"], ["--max-issues", "5"]])
def test_refresh_refuses_partial_selection(flags, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["ws/repo", "--refresh", *flags])

    assert excinfo.value.code == 2
    assert "--refresh" in capsys.readouterr().err


def test_unexpected_errors_are_logged_with_traceback(monkeypatch, caplog):
    def explode(args):
        raise ConnectionResetError("forcibly closed")

    monkeypatch.setattr("bitbucket_export.cli._run_export", explode)

    assert main(["ws/repo", "--email", "x@example.com", "--token", "t"]) == 1
    assert "Export failed with an unexpected error" in caplog.text
    assert "ConnectionResetError" in caplog.text
