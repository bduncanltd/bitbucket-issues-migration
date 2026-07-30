"""Workspace issue inventory: which repositories are counted and how the table reads."""

from __future__ import annotations

from bitbucket_export.bitbucket_client import API_ROOT, BitbucketClient
from bitbucket_export.workspace_issues import RepoIssueCount, fetch_issue_counts, main, render_table

LIST_URL = f"{API_ROOT}/repositories/ws?pagelen=100"


class _FakeClient:
    """Answers get_json from a scripted url -> payload map, recording every url fetched."""

    def __init__(self, payloads: dict[str, dict]) -> None:
        self.payloads = payloads
        self.fetched: list[str] = []

    def get_json(self, url: str) -> dict:
        self.fetched.append(url)
        return self.payloads[url]

    paginate_url = BitbucketClient.paginate_url


def _repo(slug: str, project_key: str = "PROJ", project_name: str = "Project", has_issues: bool = True) -> dict:
    return {"slug": slug, "has_issues": has_issues, "project": {"key": project_key, "name": project_name}}


def _issues_url(slug: str) -> str:
    return f"{API_ROOT}/repositories/ws/{slug}/issues?pagelen=1"


def test_repos_without_tracker_are_not_queried():
    client = _FakeClient(
        {
            LIST_URL: {"values": [_repo("untracked", has_issues=False), _repo("tracked")]},
            _issues_url("tracked"): {"size": 3},
        }
    )

    counts = fetch_issue_counts(client, "ws")

    assert [count.repo_slug for count in counts] == ["tracked"]
    assert _issues_url("untracked") not in client.fetched


def test_zero_issue_repos_are_omitted():
    client = _FakeClient(
        {
            LIST_URL: {"values": [_repo("empty"), _repo("full")]},
            _issues_url("empty"): {"size": 0},
            _issues_url("full"): {"size": 2},
        }
    )

    counts = fetch_issue_counts(client, "ws")

    assert counts == [RepoIssueCount("PROJ", "Project", "full", 2)]


def test_counts_follow_repository_pagination():
    second_page = f"{API_ROOT}/repositories/ws?pagelen=100&page=2"
    client = _FakeClient(
        {
            LIST_URL: {"values": [_repo("first")], "next": second_page},
            second_page: {"values": [_repo("second")]},
            _issues_url("first"): {"size": 1},
            _issues_url("second"): {"size": 4},
        }
    )

    counts = fetch_issue_counts(client, "ws")

    assert {count.repo_slug: count.issue_count for count in counts} == {"first": 1, "second": 4}


def test_rows_sorted_by_project_then_repo():
    client = _FakeClient(
        {
            LIST_URL: {
                "values": [
                    _repo("zeta", project_key="TOOLS"),
                    _repo("Beta", project_key="core"),
                    _repo("alpha", project_key="CORE"),
                ]
            },
            _issues_url("zeta"): {"size": 1},
            _issues_url("Beta"): {"size": 1},
            _issues_url("alpha"): {"size": 1},
        }
    )

    counts = fetch_issue_counts(client, "ws")

    assert [count.repo_slug for count in counts] == ["alpha", "Beta", "zeta"]


def test_render_table_aligns_columns_and_totals():
    counts = [
        RepoIssueCount("CORE", "Core Platform", "widget-service", 42),
        RepoIssueCount("TOOLS", "Tooling", "build-scripts", 7),
    ]

    assert render_table(counts) == (
        "Project  Project name   Repository      Issues\n"
        "CORE     Core Platform  widget-service      42\n"
        "TOOLS    Tooling        build-scripts        7\n"
        "----------------------------------------------\n"
        "2 repositories, 49 issues"
    )


def test_missing_email_aborts(monkeypatch, capsys):
    monkeypatch.delenv("BITBUCKET_EMAIL", raising=False)

    assert main(["ws", "--token", "token"]) == 1
    assert "No email provided" in capsys.readouterr().err
