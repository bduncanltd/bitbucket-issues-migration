"""Migrates issues from a Bitbucket export zip to an existing Jira project.

Requirements:
- Python packages:
    - jira
    - colorlog
    - pyyaml
    - playwright (for inline image download; run `playwright install chromium` after installing)

Create a migration_config.yaml file (see migration_config.yaml for the structure).
Do NOT commit this file — it contains a secret API token.

Usage:
    # Save Bitbucket browser session once (opens Chrome for you to log in):
    python migrate_bitbucket_to_jira.py --config migration_config.yaml --prepare-auth

    # Migrate all issues (with inline image download using saved session):
    python migrate_bitbucket_to_jira.py --config migration_config.yaml

    # Migrate first N issues only:
    python migrate_bitbucket_to_jira.py --config migration_config.yaml --limit 3
"""

import argparse
import logging
from pathlib import Path

import colorlog

from jira_migration.bitbucket_export import BitbucketExport
from jira_migration.config import JiraMigrationConfig
from jira_migration.jira_import import DEFAULT_AUTH_STATE, JiraImport
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
    parser.add_argument(
        "--limit", type=int, default=None, help="Only migrate the first N issues."
    )
    parser.add_argument(
        "--from-issue",
        type=int,
        default=1,
        dest="from_issue",
        help="Start migration from this issue number (inclusive).",
    )
    parser.add_argument(
        "--prepare-auth",
        action="store_true",
        help="Open a browser so you can log in to Bitbucket, then save auth state for image downloads.",
    )
    parser.add_argument(
        "--auth-state",
        type=Path,
        default=DEFAULT_AUTH_STATE,
        help=f"Playwright auth state file for Bitbucket image downloads. Default: {DEFAULT_AUTH_STATE}",
    )
    args = parser.parse_args()

    if args.prepare_auth:
        from playwright.sync_api import sync_playwright  # type: ignore[import]

        args.auth_state.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto("https://bitbucket.org/account/signin/", wait_until="load")
            print("Sign in to Bitbucket in the browser window.")
            print("When fully logged in, press Enter here to save auth state.")
            input()
            context.storage_state(path=str(args.auth_state))
            context.close()
            browser.close()
        logging.info("Saved auth state to %s", args.auth_state)
    else:
        auth_state = args.auth_state if args.auth_state.exists() else None
        if auth_state is None:
            logging.warning(
                "No Playwright auth state found — inline images will be skipped. Run with --prepare-auth first."
            )

        config = JiraMigrationConfig.from_yaml(args.config)
        bitbucket_export = BitbucketExport(config.export_zip)
        jira_import = JiraImport(config, auth_state=auth_state)

        migrator = BitbucketJiraMigrator(
            export=bitbucket_export, jira=jira_import, config=config
        )
        migrator.migrate(limit=args.limit, from_issue=args.from_issue)
