# Bitbucket Issues Migration

[![CI](https://github.com/bduncanltd/bitbucket-issues-migration/actions/workflows/ci.yml/badge.svg)](https://github.com/bduncanltd/bitbucket-issues-migration/actions/workflows/ci.yml)

Scripts for migrating Bitbucket issues to a Jira Cloud project.

> **Disclaimer:** These scripts were generated with AI assistance. They have been tested on a real migration, but may not handle every edge case. Review the configuration and mappings carefully before running against a production Jira project, and use at your own risk.

> **Maintenance notice:** This repository is **not actively maintained**. It is shared as a starting point — feel free to use it as a base, adapt it with AI assistance for your own repository's structure, and open questions in the Issues tab if you get stuck.

## Overview

Three tools are provided, each with one job:

- **[bitbucket_export](bitbucket_export/README.md)** — Exports a repository's issues, comments, history, attachments, and inline images into a reusable archive.
- **[static_site](static_site/README.md)** — Turns an archive into a static HTML site.
- **Jira Migration Scripts** — Migrates Bitbucket issues into a Jira Cloud project (see below).

The older **[export_archive](export_archive/README.md)** tool, which built an HTML
archive directly from a Bitbucket admin export zip, is kept for reference. The
`bitbucket_export` + `static_site` pair supersedes it.

---

## Exporting and Publishing Issues

```bash
python -m bitbucket_export --prepare-auth                          # once
python -m bitbucket_export workspace/repo --email you@example.com  # export
python -m static_site .archive/workspace/repo                      # publish
```

The export lands in `.archive/<workspace>/<repo>` and the site in
`.site/<workspace>/<repo>`. Set `$BITBUCKET_EMAIL` and the export shortens to
`python -m bitbucket_export workspace/repo`.

The exporter is the only step that touches the network, and it gets everything in one
pass into a self-describing archive: one JSON file per issue under `issues/`, plus a
`manifest.json` of shared lookup tables and content-addressed `assets/`.
It knows nothing about HTML.

The site generator reads that archive offline. It is one consumer of the
[documented schema](bitbucket_export/README.md#the-schema), not a privileged part of
it — a Jira importer or a search index would read the same archive the same way.

Re-running the export is incremental: it reuses stored assets, so resuming an
interrupted run, retrying failures, or picking up new issues costs almost nothing.

---

## Jira Migration Scripts

Two scripts are provided:

- **`migrate_bitbucket_to_jira.py`** — Migrates issues (with comments, attachments, and inline images) from a Bitbucket issue archive into Jira.
- **`migrate_bitbucket_components_to_jira.py`** — Creates Jira components from the archive and assigns them to the migrated issues.

Both read the archive created by `bitbucket_export`, so the only network access they
need is to Jira — the migration keeps working after Bitbucket issues shut down.

Re-running the migration will often update existing issues rather than create duplicates, which can help when resuming from a specific issue. However, the migration is **not fully idempotent**: comment synchronization may delete previously synced or other "leftover" Jira comments, so review results carefully and do not assume re-runs are risk-free.

---

## Prerequisites

**Python 3.11+** is required.

Install dependencies:

```bash
pip install -r requirements.txt
playwright install chromium
```

The `playwright install chromium` step downloads the Chromium browser used by `bitbucket_export --prepare-auth` to authenticate with Bitbucket and fetch inline images into the archive. The Jira migration itself needs no browser.

---

## Configuration

Copy the template to create your local config file (which is gitignored to prevent accidental secret commits):

```bash
cp migration_config.example.yaml migration_config.yaml
```

Then fill in `migration_config.yaml` with your values:

```yaml
bitbucket:
  archive-dir: ".archive/workspace/repo"

jira:
  url: "https://your-site.atlassian.net/"
  email: "your-email@example.com"
  api-token: "your-jira-api-token"
  board-id: "PROJ"
```

| Field | Description |
|---|---|
| `bitbucket.archive-dir` | Archive directory created by `python -m bitbucket_export` |
| `jira.url` | Your Jira Cloud URL (must end with `/`) |
| `jira.email` | Email address associated with your Jira account |
| `jira.api-token` | Jira API token — generate one under **Jira → Profile → Security → API Tokens** |
| `jira.board-id` | Jira project key (e.g. `PROJ`). Issues are created as `PROJ-1`, `PROJ-2`, etc. |

### Generating a Jira API Token

1. Log in to Jira Cloud.
2. Go to **Profile → Security → Create and manage API tokens**.
3. Create a new token and paste it into `migration_config.yaml`.

### Creating the Archive

Export the repository's issue tracker first (see [`bitbucket_export`](bitbucket_export/README.md)):

```bash
python -m bitbucket_export --prepare-auth                          # once
python -m bitbucket_export workspace/repo --email you@example.com
```

Then set `bitbucket.archive-dir` in `migration_config.yaml` to the resulting `.archive/workspace/repo`.

---

## Running the Migration

```bash
python migrate_bitbucket_to_jira.py --config migration_config.yaml
```

This will:

1. Validate the Jira project exists and log any Bitbucket users not found in Jira.
2. Create any Bitbucket components missing from the Jira project, up front.
3. For each issue: create it in Jira with its component set (or update it if it already exists).
4. Upload archived inline images as Jira attachments and rewrite the markup to use them.
5. Upload file attachments from the archive.
6. Sync comments.
7. Transition the issue to the correct status.

**Optional flags:**

| Flag | Description |
|---|---|
| `--limit N` | Only migrate the first N issues (useful for testing) |
| `--from-issue N` | Start from issue number N (useful for resuming after a failure) |

**Example — test with 5 issues, then run the full migration from issue 6:**

```bash
python migrate_bitbucket_to_jira.py --config migration_config.yaml --limit 5
python migrate_bitbucket_to_jira.py --config migration_config.yaml --from-issue 6
```

### Migrating Components

```bash
python migrate_bitbucket_components_to_jira.py --config migration_config.yaml
```

The main migration already creates components and assigns them at issue creation, so this script is only needed to backfill issues migrated by an older version of the script.

It reads component data from the archive, creates any missing components in Jira, and updates each issue's component field.

Use `--dry-run` to preview actions without making changes:

```bash
python migrate_bitbucket_components_to_jira.py --config migration_config.yaml --dry-run
```

---

## What You May Need to Customise

### Mapping Files in `jira_migration/migrator.py`

The migration uses several hardcoded maps that translate Bitbucket's native vocabulary (as recorded in the archive) to Jira values. The Bitbucket side is fixed — kinds, states, and priorities are Bitbucket's own — but the Jira side must match your project, so review the target values before running.

#### `ISSUE_TYPE_MAP`

Maps Bitbucket issue kinds to Jira issue types. Jira issue types must exactly match what exists in your Jira project.

```python
ISSUE_TYPE_MAP = {
    "bug": "Bug",
    "enhancement": "Task",
    "proposal": "Task",
    "task": "Task",
}
```

If the migration encounters a kind not in this map, it will log an error and exit. Change the mapped Jira values to match your project's issue types (e.g. `"Story"` if your Jira project has a Story type).

#### `ISSUE_STATUS_MAP`

Maps Bitbucket issue states to Jira statuses. Jira status names must exactly match valid transitions in your Jira workflow. Bitbucket's resolution-like states (`invalid`, `duplicate`, `wontfix`) are ordinary states in this map.

```python
ISSUE_STATUS_MAP = {
    "new": "To Do",
    "open": "In Progress",
    "on hold": "To Do",
    "resolved": "Done",
    "closed": "Done",
    "invalid": "Invalid",
    "duplicate": "Invalid",
    "wontfix": "Invalid",
}
```

If the migration encounters a state not in this map, it will log an error and exit. The target statuses (e.g. `"Invalid"`) must exist as valid transitions in your Jira workflow.

#### `PRIORITY_MAP`

Maps Bitbucket priorities to Jira priority IDs. Priority IDs are numeric and vary between Jira instances — check your Jira project's priority list if issues are created with the wrong priority.

```python
PRIORITY_MAP = {
    "blocker": "1",
    "critical": "4",
    "major": "2",
    "minor": "3",
    "trivial": "5",
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
- **Descriptions** are appended with a metadata footer showing a link to the original Bitbucket issue, its state, kind, priority, assignee, reporter, component/milestone/version, and timestamps.
- **Comments** cannot preserve the original author or timestamp in Jira — all migrated comments appear as posted by the API user at the time of migration. To work around this, each comment is prefixed with a metadata line showing the original Bitbucket author and timestamp. Bitbucket's empty field-change comments are not migrated.
- **Inline images** (e.g. screenshots pasted into descriptions) are taken from the archive and uploaded to Jira as attachments, replacing the original URLs. Images the archive could not capture are left as their original URLs, with a warning.
- **File attachments** are uploaded from the archive. Already-uploaded files (matched by filename) are not re-uploaded.
- **User mentions** (`@{account-id}` in Bitbucket markdown) become real Jira mentions for users that exist in Jira — the account ids are the same on both sides — and plain display names for everyone else. Real mentions notify the mentioned user unless the project's notification scheme is disabled during migration.
- **Markdown** in descriptions and comments is converted to Jira Wiki Markup. Bodies written in creole or plaintext (which old Bitbucket trackers contain) are preserved verbatim in `{noformat}` blocks instead of being mis-parsed as Markdown.

---

## Troubleshooting

**`Unknown issue kind '...'` / `Unknown issue state '...'` errors**
Update `ISSUE_TYPE_MAP` or `ISSUE_STATUS_MAP` in `jira_migration/migrator.py` to include the missing value.

**Users not found in Jira**
Logged as warnings at startup. Affected issues are created without assignee/reporter. No action is required unless assignment is critical.

**Inline images or attachments not migrated**
The archive never captured them — they are listed under `unresolved_assets` in the archive's `manifest.json`. Re-run `python -m bitbucket_export` (it retries only what is missing), then re-run the migration.

**Migration stops partway through**
Resume from the failed issue using `--from-issue N`. Already-migrated issues will be updated rather than duplicated.

**Jira API rate limits or propagation delays**
The script includes automatic retry logic with exponential backoff for Jira Cloud. If you see persistent failures, wait a few minutes and re-run with `--from-issue`.
