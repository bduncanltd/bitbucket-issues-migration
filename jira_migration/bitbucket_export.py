"""Loads and normalises a Bitbucket export zip file."""

from __future__ import annotations

import json
import logging
import shutil
import zipfile
from pathlib import Path
from typing import Any

from jira_migration.bitbucket_issue import BitbucketAttachment, BitbucketIssue


def _extract_zip(zip_path: str) -> Path:
    """Extract the zip to .migration/<zip-stem>/, deleting any previous extraction."""
    extract_dir = Path(".migration") / Path(zip_path).stem
    if extract_dir.exists():
        logging.info("Deleting previous extraction at %s", extract_dir)
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)
    logging.info("Extracting %s to %s", zip_path, extract_dir)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)
    return extract_dir


def _find_export_json(extract_dir: Path, filename: str) -> Path:
    path = extract_dir / filename
    if not path.exists():
        raise RuntimeError(f"Could not find {filename} in {extract_dir}")
    return path


def _load_db1(extract_dir: Path) -> dict:
    return json.loads((extract_dir / "db-1.0.json").read_text(encoding="utf-8"))


def _load_attachment_map(
    db1: dict, extract_dir: Path
) -> dict[int, list[BitbucketAttachment]]:
    """Build a map of issue_id → list of attachments from db-1.0.json."""
    result: dict[int, list[BitbucketAttachment]] = {}
    for entry in db1.get("attachments", []):
        issue_id: int = entry["issue"]
        path = str(extract_dir / entry["path"])
        attachment = BitbucketAttachment(filename=entry["filename"], path=path)
        result.setdefault(issue_id, []).append(attachment)
    return result


def _build_display_name_map(db1: dict, raw_issues: list[dict]) -> dict[str, str]:
    """Build account_id → username map by correlating db-1.0.json with db-jira-cloud.json."""
    ts_to_user: dict[str, str] = {
        c["created_on"]: c["user"] for c in db1.get("comments", [])
    }
    id_to_reporter: dict[int, str] = {
        i["id"]: i["reporter"] for i in db1.get("issues", []) if i.get("reporter")
    }
    id_to_assignee: dict[int, str] = {
        i["id"]: i["assignee"] for i in db1.get("issues", []) if i.get("assignee")
    }

    result: dict[str, str] = {}
    for issue in raw_issues:
        ext_id = str(issue.get("externalId", ""))
        issue_id = int(ext_id.rsplit("-", maxsplit=1)[-1]) if ext_id else None
        if issue_id is not None:
            if (reporter := issue.get("reporter")) and issue_id in id_to_reporter:
                result[reporter] = id_to_reporter[issue_id]
            if (assignee := issue.get("assignee")) and issue_id in id_to_assignee:
                result[assignee] = id_to_assignee[issue_id]
        for comment in issue.get("comments", []):
            if (
                (author := comment.get("author"))
                and (ts := comment.get("created"))
                and ts in ts_to_user
            ):
                result[author] = ts_to_user[ts]
    return result


class BitbucketExport:
    def __init__(self, zip_path: str) -> None:
        extract_dir = _extract_zip(zip_path)

        bb_export = json.loads(
            _find_export_json(extract_dir, "db-jira-cloud.json").read_text(
                encoding="utf-8"
            )
        )
        projects = bb_export["projects"]
        assert isinstance(projects, list)
        assert len(projects) == 1

        db1 = _load_db1(extract_dir)
        attachment_map = _load_attachment_map(db1, extract_dir)

        raw_issues: list[dict[str, Any]] = projects[0]["issues"]
        self.user_display_names = _build_display_name_map(db1, raw_issues)
        self.issues = [BitbucketIssue.from_dict(issue) for issue in raw_issues]
        for issue in self.issues:
            issue.attachments = attachment_map.get(issue.id, [])

        logging.info("Found %d issues.", len(self.issues))
        self._insert_missing_issues()
        self._assert_ids_are_in_order()

    @property
    def user_ids(self) -> set[str]:
        """All reporter and assignee user IDs found across all issues."""
        ids: set[str] = set()
        for issue in self.issues:
            if issue.reporter:
                ids.add(issue.reporter)
            if issue.assignee:
                ids.add(issue.assignee)
        return ids

    def _insert_missing_issues(self) -> None:
        """Fill gaps left by deleted Bitbucket issues with dummy placeholders."""
        logging.info("Checking for missing issues...")
        expected = 1
        missing: list[int] = []
        for issue in self.issues:
            while expected < issue.id:
                missing.append(expected)
                expected += 1
            expected += 1

        for n in missing:
            logging.warning("Missing issue with ID %d", n)

        for n in missing:
            self.issues.insert(
                n - 1,
                BitbucketIssue(
                    id=n,
                    type="Task",
                    status="Done",
                    resolution="invalid",
                    summary=f"Dummy issue {n}",
                    description="Dummy issue to preserve issue numbering during Bitbucket to Jira migration.",
                    priority="Lowest",
                    comments=[],
                    attachments=[],
                    created="2024-08-22T03:33:15.522982+00:00",
                    updated="2024-08-22T03:33:15.522982+00:00",
                ),
            )
        logging.info("There are now %d issues.", len(self.issues))

    def _assert_ids_are_in_order(self) -> None:
        expected = 1
        for issue in self.issues:
            assert issue.id == expected
            expected += 1
