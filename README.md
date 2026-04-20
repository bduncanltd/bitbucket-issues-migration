# Bitbucket Issues Migration

Scripts for migrating Bitbucket issues to a Jira Cloud project.

> **Disclaimer:** These scripts were generated with AI assistance. They have been tested on a real migration, but may not handle every edge case. Review the configuration and mappings carefully before running against a production Jira project, and use at your own risk.

## Overview

Two scripts are provided:

- **`migrate_bitbucket_to_jira.py`** — Migrates issues (with comments, attachments, and inline images) from a Bitbucket export zip into Jira.
- **`migrate_bitbucket_components_to_jira.py`** — Creates Jira components from the Bitbucket export and assigns them to the migrated issues.

The migration is **idempotent** — re-running it will update existing issues rather than create duplicates, so it is safe to run multiple times or resume from a specific issue.

---

## Prerequisites

**Python 3.10+** is required.

Install dependencies:

```bash
pip install -r requirements.txt
playwright install chromium
```

The `playwright install chromium` step downloads the Chromium browser used to authenticate with Bitbucket and download inline images embedded in issue descriptions and comments.

---

## Configuration

Fill in `migration_config.yaml` (already committed as a template — keep your local changes from being committed by running):

```bash
git update-index --skip-worktree migration_config.yaml
```


```yaml
bitbucket:
  export-zip: "your_bitbucket_export-issues.zip"

jira:
  url: "https://your-site.atlassian.net/"
  email: "your-email@example.com"
  api-token: "your-jira-api-token"
  board-id: "PROJ"
```

| Field | Description |
|---|---|
| `bitbucket.export-zip` | Path to the Bitbucket issues export zip file |
| `jira.url` | Your Jira Cloud URL (must end with `/`) |
| `jira.email` | Email address associated with your Jira account |
| `jira.api-token` | Jira API token — generate one under **Jira → Profile → Security → API Tokens** |
| `jira.board-id` | Jira project key (e.g. `PROJ`). Issues are created as `PROJ-1`, `PROJ-2`, etc. |

### Generating a Jira API Token

1. Log in to Jira Cloud.
2. Go to **Profile → Security → Create and manage API tokens**.
3. Create a new token and paste it into `migration_config.yaml`.

### Obtaining a Bitbucket Export

In Bitbucket, go to your repository → **Settings → Issues → Import & export** → **Export issues**. Download the resulting zip file and set its path in `migration_config.yaml`.

---

## Running the Migration

### Step 1 — Authenticate with Bitbucket (for inline images)

Issue descriptions and comments may contain images hosted on Bitbucket. To download and re-upload these to Jira, you must first save a Bitbucket browser session:

```bash
python migrate_bitbucket_to_jira.py --config migration_config.yaml --prepare-auth
```

This opens a Chrome window. Log in to Bitbucket, then close the window. Your session is saved to `~/.cache/bitbucket-playwright/auth-state.json`.

> **Skip this step** if your issues contain no inline images, or if you do not need them migrated.

### Step 2 — Migrate Issues

```bash
python migrate_bitbucket_to_jira.py --config migration_config.yaml
```

This will:

1. Validate the Jira project exists and log any Bitbucket users not found in Jira.
2. For each issue: create it in Jira (or update it if it already exists).
3. Download any inline images from Bitbucket and re-upload them as Jira attachments.
4. Upload file attachments.
5. Sync comments.
6. Transition the issue to the correct status.

**Optional flags:**

| Flag | Description |
|---|---|
| `--limit N` | Only migrate the first N issues (useful for testing) |
| `--from-issue N` | Start from issue number N (useful for resuming after a failure) |
| `--auth-state PATH` | Path to Playwright auth state file (default: `~/.cache/bitbucket-playwright/auth-state.json`) |

**Example — test with 5 issues, then run the full migration from issue 6:**

```bash
python migrate_bitbucket_to_jira.py --config migration_config.yaml --limit 5
python migrate_bitbucket_to_jira.py --config migration_config.yaml --from-issue 6
```

### Step 3 — Migrate Components

```bash
python migrate_bitbucket_components_to_jira.py --config migration_config.yaml
```

This reads component data from the Bitbucket export, creates any missing components in Jira, and updates each issue's component field.

Use `--dry-run` to preview actions without making changes:

```bash
python migrate_bitbucket_components_to_jira.py --config migration_config.yaml --dry-run
```

---

## What You May Need to Customise

### Mapping Files in `jira_migration/migrator.py`

The migration uses several hardcoded maps that translate Bitbucket values to Jira values. If your Bitbucket project uses different issue types, statuses, or priorities, you must update these maps before running the migration.

#### `ISSUE_TYPE_MAP`

Maps Bitbucket issue types to Jira issue types. Jira issue types must exactly match what exists in your Jira project.

```python
ISSUE_TYPE_MAP = {
    "Story": "Task",
    "Task": "Task",
    "Bug": "Bug",
}
```

If the migration encounters an issue type not in this map, it will log an error and exit. Add any missing types, or change the mapped Jira values to match your project's issue types (e.g. `"Story"` if your Jira project has a Story type).

#### `ISSUE_STATUS_MAP`

Maps Bitbucket issue statuses to Jira statuses. Jira status names must exactly match valid transitions in your Jira workflow.

```python
ISSUE_STATUS_MAP = {
    "Done": "Done",
    "Selected For Development": "In Progress",
    "Backlog": "To Do",
}
```

If the migration encounters a status not in this map, it will log an error and exit. Check your Bitbucket export for all status values and ensure each is mapped.

#### `RESOLUTION_STATUS_MAP`

When a Bitbucket issue has a resolution set, this map overrides the status. This is used to map closed/rejected issues to appropriate Jira statuses.

```python
RESOLUTION_STATUS_MAP = {
    "invalid": "Invalid",
    "duplicate": "Invalid",
    "wontfix": "Invalid",
    "won't fix": "Invalid",
    "resolved": "Done",
}
```

The target statuses (`"Invalid"`, `"Done"`) must exist as valid transitions in your Jira workflow.

#### `PRIORITY_MAP`

Maps Bitbucket priority names to Jira priority IDs. Priority IDs are numeric and vary between Jira instances — check your Jira project's priority list if issues are created with the wrong priority.

```python
PRIORITY_MAP = {
    "Highest": "1",
    "Medium": "2",
    "Low": "3",
    "High": "4",
    "Lowest": "5",
}
```

### User Mapping

The migration matches Bitbucket user IDs directly to Jira account IDs. Any users that cannot be matched are logged at startup with a count of affected issues — those issues will be created without an assignee or reporter.

If users cannot be matched, check that:
- The user exists in Jira with the same account ID used in Bitbucket.
- The user is a member of a Jira group that is visible to the API.

### Jira Workflow Status Transitions

The migration attempts to transition each issue to its target status after creation. The status name in the maps above must match a reachable transition name in your Jira project workflow. If a transition cannot be found, the issue will remain in its default initial status and a warning will be logged.

---

## How Issues Are Migrated

- **Issue keys** are set to `<board-id>-<bitbucket-id>` (e.g. `PROJ-42`), preserving the original Bitbucket issue numbers.
- **Deleted Bitbucket issues** create placeholder issues to keep numbering intact.
- **Descriptions** are prepended with a metadata header showing the original Bitbucket status, resolution, type, assignee, reporter, and timestamps.
- **Comments** cannot preserve the original author or timestamp in Jira — all migrated comments appear as posted by the API user at the time of migration. To work around this, each comment is prefixed with a metadata line showing the original Bitbucket author and timestamp.
- **Inline images** (e.g. screenshots pasted into descriptions) are downloaded from Bitbucket and re-uploaded to Jira as attachments, replacing the original URLs.
- **File attachments** are uploaded to the Jira issue. Already-uploaded files (matched by filename) are not re-uploaded.
- **Markdown** in descriptions and comments is converted to Jira Wiki Markup.

---

## Troubleshooting

**`Unknown issue type '...'` / `Unknown issue status '...'` errors**
Update `ISSUE_TYPE_MAP` or `ISSUE_STATUS_MAP` in `jira_migration/migrator.py` to include the missing value.

**Users not found in Jira**
Logged as warnings at startup. Affected issues are created without assignee/reporter. No action is required unless assignment is critical.

**Inline images not migrated**
Run `--prepare-auth` and log in to Bitbucket before running the migration.

**Migration stops partway through**
Resume from the failed issue using `--from-issue N`. Already-migrated issues will be updated rather than duplicated.

**Jira API rate limits or propagation delays**
The script includes automatic retry logic with exponential backoff for Jira Cloud. If you see persistent failures, wait a few minutes and re-run with `--from-issue`.
