# Bitbucket Issue Exporter

This toolset converts a Bitbucket Issues JSON export into a static HTML archive and helps recover inline images that are not included in the export bundle.

## What This Is For

The main purpose of this tool is to turn a Bitbucket Issues backup into a future-proof, read-only historical record.

The final output is a static website:

- browsable offline from local files
- suitable for hosting on GitHub Pages or another static host
- useful as a historical reference after Bitbucket Issues is shut down
- linkable from newer Jira, GitHub, or other ticket systems

It is not a replacement ticket server. The generated archive does not support creating, editing, or commenting on issues.

## Main Input

The main input to the process is the Bitbucket Issues backup for a repository.

In practice this usually starts as a downloaded zip file from Bitbucket. After extracting it, the tool expects a folder containing:

- `db-1.0.json`
- `attachments/`

That extracted backup is the source of truth for issue metadata, comments, logs, and named attachments.

## What It Handles

- Generates `index.html` and one HTML page per issue
- Can do a lightweight first pass that only produces `missing-inline-images.csv`
- Copies named issue attachments into the archive
- Rewrites Bitbucket issue links to local HTML links where possible
- Produces `missing-inline-images.csv` for inline images that were referenced but not present locally
- Downloads missing Bitbucket-hosted inline images using a saved authenticated Playwright session
- Appends `migration.log` in the project directory so each export/download run is documented

## Files

- `export_archive.py`: generate the HTML archive from the Bitbucket export bundle
- `rendering.py`: HTML page generation and markdown-to-HTML rendering helpers
- `download_inline_images_playwright.py`: download missing inline images from `missing-inline-images.csv`
- `assets/style.css`: shared CSS for generated archive pages

## Suggested Workspace Layout

Using one folder per repository keeps repeated runs tidy:

```text
~/bb_projects/<repo-slug>/
  bitbucket-export/
  inline_image_report/
  missing_aws_images/
  bitbucket_issue_archive_final/
  migration.log
```

## Typical Migration Flow

### 1. Obtain And Unpack The Bitbucket Backup

Download the repository issues backup zip from Bitbucket and unpack it so you have a directory containing `db-1.0.json` and `attachments/`.

Example:

```text
~/bb_projects/example-repo/bitbucket-export
```

### 2. Generate The Missing-Image Report

```bash
python export_archive/export_archive.py \
  ~/bb_projects/<repo-slug>/bitbucket-export \
  ~/bb_projects/<repo-slug>/inline_image_report \
  --report-only
```

If some inline images were already manually saved elsewhere, include them now:

```bash
python export_archive/export_archive.py \
  ~/bb_projects/<repo-slug>/bitbucket-export \
  ~/bb_projects/<repo-slug>/inline_image_report \
  --report-only \
  --inline-image-dir /path/to/manually-saved-inline-images
```

This first pass will usually produce:

- `missing-inline-images.csv`

It does not generate HTML pages or copy attachments. It is just a lightweight scan of the Bitbucket backup to determine which inline images still need to be recovered.

### 3. Save Authenticated Playwright State

Install Playwright if needed:

```bash
pip install playwright
python -m playwright install chromium
```

Save an authenticated Bitbucket session:

```bash
python export_archive/download_inline_images_playwright.py \
  --prepare-auth \
  --auth-state ~/.cache/bitbucket-playwright/auth-state.json \
  --channel chromium
```

This opens a browser so you can log in manually, including 2FA if required.

This online login step is optional. You only need it if you want to recover missing inline images that were not included in the Bitbucket backup.

The downloader uses your own authenticated Bitbucket session, so it can only recover images that your account can still access.

### 4. Download Missing Inline Images

```bash
python export_archive/download_inline_images_playwright.py \
  --csv ~/bb_projects/<repo-slug>/inline_image_report/missing-inline-images.csv \
  --auth-state ~/.cache/bitbucket-playwright/auth-state.json \
  --output-dir ~/bb_projects/<repo-slug>/missing_aws_images \
  --concurrency 2 \
  --request-timeout-ms 120000 \
  --channel chromium
```

This writes:

- recovered image files into `/path/to/missing_aws_images`
- recovered image files into `~/bb_projects/<repo-slug>/missing_aws_images`
- `download-results.csv` with per-image status

The downloader's results file preserves the original `missing-inline-images.csv` columns, so it can be reused as input for another run.

If a few Bitbucket-hosted images still fail due to timeouts, you can retry just the failed rows directly from `download-results.csv`:

```bash
python export_archive/download_inline_images_playwright.py \
  --csv ~/bb_projects/<repo-slug>/missing_aws_images/download-results.csv \
  --retry-failed-only \
  --auth-state ~/.cache/bitbucket-playwright/auth-state.json \
  --output-dir ~/bb_projects/<repo-slug>/missing_aws_images \
  --concurrency 2 \
  --request-timeout-ms 300000 \
  --channel chromium
```

The downloader defaults to a small worker count so one slow file does not block the whole run. Increase `--concurrency` carefully if your Bitbucket instance is stable under load.

Both the exporter and downloader append a `migration.log` file in the project directory by default.

### 5. Re-Run The Archive Using All Available Image Sources

Pass `--inline-image-dir` multiple times to combine manually saved images and downloaded Bitbucket images:

```bash
python export_archive/export_archive.py \
  ~/bb_projects/<repo-slug>/bitbucket-export \
  ~/bb_projects/<repo-slug>/bitbucket_issue_archive_final \
  --archive-title "Repository Bitbucket Archive" \
  --repo-slug workspace/repository \
  --inline-image-dir /path/to/manually-saved-inline-images \
  --inline-image-dir ~/bb_projects/<repo-slug>/missing_aws_images
```

If everything worked, the new archive should contain far fewer entries in `missing-inline-images.csv`.
In the common case, the final archive will not have a `missing-inline-images.csv` at all.

### 6. Publish Or Reference The Archive

The final archive is a static read-only website. You can:

- keep it locally as an offline record
- host it on GitHub Pages or another static web host
- link to individual archived issue pages from Jira, GitHub Issues, or other migrated ticket systems

## Notes

- Named issue attachments are taken from the Bitbucket export bundle directly.
- Many inline images are not included in the Bitbucket export bundle and must be recovered separately.
- The Playwright downloader uses the original `image_url` values from `missing-inline-images.csv` and follows Bitbucket's authenticated redirect to the signed asset URL.
- Some failures may remain, for example timeouts or non-Bitbucket external image links.
