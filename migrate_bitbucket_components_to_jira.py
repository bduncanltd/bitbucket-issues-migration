"""Reads a Bitbucket issue archive and:
1. Creates Jira components (if they don't already exist) on the target project.
2. Updates the component field of every Jira issue that has a component in the archive.

The archive is created by: python -m bitbucket_export workspace/repo

Usage:
    python migrate_bitbucket_components_to_jira.py --config migration_config.yaml

    # Dry-run (log actions without making any changes):
    python migrate_bitbucket_components_to_jira.py --config migration_config.yaml --dry-run
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

import colorlog
import yaml
from jira import JIRA

from bitbucket_export.model import Archive


def load_config(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        result: dict[str, Any] = yaml.safe_load(f)
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate Bitbucket components to Jira.")
    parser.add_argument("--config", required=True, help="Path to migration_config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Log actions without making any changes")
    args = parser.parse_args()

    config = load_config(args.config)
    jira_cfg = config["jira"]
    project: str = jira_cfg["board-id"]

    try:
        archive = Archive.load(Path(config["bitbucket"]["archive-dir"]))
    # FileNotFoundError: no manifest; ValueError: archive written by an unsupported schema.
    except (FileNotFoundError, ValueError) as error:
        logging.error("%s\nCreate the archive first: python -m bitbucket_export <workspace/repo>", error)
        raise SystemExit(1) from error

    id_to_component: dict[int, str] = {issue.id: issue.component for issue in archive.issues if issue.component}
    # The manifest keeps component definitions even when no issue uses them.
    all_components = set(id_to_component.values()) | {c.name for c in archive.components}

    client = JIRA(server=jira_cfg["url"], basic_auth=(jira_cfg["email"], jira_cfg["api-token"]))

    existing_components = {c.name for c in client.project_components(project)}
    for name in sorted(all_components):
        if name not in existing_components:
            logging.info("Creating component: %s", name)
            if not args.dry_run:
                client.create_component(name, project)

    for issue_id, component_name in sorted(id_to_component.items()):
        issue_key = f"{project}-{issue_id}"
        logging.info("%s -> %s", issue_key, component_name)
        if not args.dry_run:
            client.issue(issue_key).update(fields={"components": [{"name": component_name}]})


if __name__ == "__main__":
    logging_handler = colorlog.StreamHandler()
    logging_handler.setFormatter(colorlog.ColoredFormatter("%(log_color)s%(message)s"))
    logging.getLogger().addHandler(logging_handler)
    logging.getLogger().setLevel(logging.INFO)

    main()
