"""Migrates issues from a Bitbucket issue archive to an existing Jira project.

The archive is created by the exporter, which captures issues, comments, attachments,
and inline images in one pass:

    python -m bitbucket_export workspace/repo --email you@example.com

Point migration_config.yaml at the archive directory (copy
migration_config.example.yaml and fill in your values), then:

    # Migrate all issues:
    python migrate_bitbucket_to_jira.py --config migration_config.yaml

    # Migrate first N issues only:
    python migrate_bitbucket_to_jira.py --config migration_config.yaml --limit 3

This script talks only to Jira; everything it needs from Bitbucket is already in the
archive.
"""

import argparse
import logging
from pathlib import Path

import colorlog

from jira_migration.archive_source import ArchiveSource
from jira_migration.config import JiraMigrationConfig
from jira_migration.jira_import import JiraImport
from jira_migration.migrator import BitbucketJiraMigrator

if __name__ == "__main__":
    logging_handler = colorlog.StreamHandler()
    logging_handler.setFormatter(colorlog.ColoredFormatter("%(log_color)s%(message)s"))
    logging.getLogger().addHandler(logging_handler)
    logging.getLogger().setLevel(logging.INFO)

    parser = argparse.ArgumentParser(description="Migrate Bitbucket issues to Jira.")
    parser.add_argument(
        "--config",
        type=str,
        required=True,
        help="Path to the migration YAML config file.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Only migrate the first N issues.")
    parser.add_argument(
        "--from-issue",
        type=int,
        default=1,
        dest="from_issue",
        help="Start migration from this issue number (inclusive).",
    )
    args = parser.parse_args()

    config = JiraMigrationConfig.from_yaml(args.config)
    try:
        source = ArchiveSource(Path(config.archive_dir))
    # FileNotFoundError: no manifest; ValueError: archive written by an unsupported schema.
    except (FileNotFoundError, ValueError) as error:
        logging.error("%s\nCreate the archive first: python -m bitbucket_export <workspace/repo>", error)
        raise SystemExit(1) from error

    jira_import = JiraImport(config)
    migrator = BitbucketJiraMigrator(export=source, jira=jira_import, config=config)
    migrator.migrate(limit=args.limit, from_issue=args.from_issue)
