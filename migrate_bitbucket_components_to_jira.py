"""Reads a Bitbucket export zip and:
1. Creates Jira components (if they don't already exist) on the target project.
2. Updates the component field of every Jira issue that has a component in the export.

Usage:
    python migrate_components.py --config migration_config.yaml

    # Dry-run (log actions without making any changes):
    python migrate_components.py --config migration_config.yaml --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import zipfile
from pathlib import Path
from typing import Any

import colorlog
import yaml
from jira import JIRA


def load_config(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        result: dict[str, Any] = yaml.safe_load(f)
        return result


def extract_zip(zip_path: str) -> Path:
    extract_dir = Path(".migration") / Path(zip_path).stem
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)
    return extract_dir


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate Bitbucket components to Jira."
    )
    parser.add_argument("--config", required=True, help="Path to migration_config.yaml")
    parser.add_argument(
        "--dry-run", action="store_true", help="Log actions without making any changes"
    )
    args = parser.parse_args()

    config = load_config(args.config)
    jira_cfg = config["jira"]
    project: str = jira_cfg["board-id"]

    extract_dir = extract_zip(config["bitbucket"]["export-zip"])
    db1: dict = json.loads((extract_dir / "db-1.0.json").read_text(encoding="utf-8"))

    id_to_component: dict[int, str] = {
        issue["id"]: issue["component"]
        for issue in db1.get("issues", [])
        if issue.get("component")
    }
    all_components = set(id_to_component.values()) | {
        c["name"] for c in db1.get("components", [])
    }

    client = JIRA(
        server=jira_cfg["url"], basic_auth=(jira_cfg["email"], jira_cfg["api-token"])
    )

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
            client.issue(issue_key).update(
                fields={"components": [{"name": component_name}]}
            )


if __name__ == "__main__":
    logging_handler = colorlog.StreamHandler()
    logging_handler.setFormatter(colorlog.ColoredFormatter("%(log_color)s%(message)s"))
    logging.getLogger().addHandler(logging_handler)
    logging.getLogger().setLevel(logging.INFO)

    main()
