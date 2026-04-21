"""Configuration for the Bitbucket to Jira migration script."""

from __future__ import annotations

from dataclasses import dataclass

import yaml


@dataclass
class JiraMigrationConfig:
    export_zip: str
    jira_url: str
    jira_email: str
    jira_api_token: str
    board_id: str

    @classmethod
    def from_yaml(cls, path: str) -> JiraMigrationConfig:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        jira_config = data["jira"]
        return cls(
            export_zip=data["bitbucket"]["export-zip"],
            jira_url=jira_config["url"],
            jira_email=jira_config["email"],
            jira_api_token=jira_config["api-token"],
            board_id=jira_config["board-id"],
        )
