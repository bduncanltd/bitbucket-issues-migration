# Bitbucket Export

Exports a Bitbucket repository's issue tracker into a reusable archive. That is all it
does — it has no opinion about what you do with the result.

```bash
# once, to authorise inline-image downloads
python -m bitbucket_export --prepare-auth

# export everything
python -m bitbucket_export workspace/repo --email you@example.com
```

One command, one pass. Issues, comments, change history, attachments, and inline images
all land in `.archive/workspace/repo`, derived from the repository you asked for.

Set `$BITBUCKET_EMAIL` (and optionally `$BITBUCKET_API_TOKEN`) and it becomes
`python -m bitbucket_export workspace/repo`.

Not sure which repositories are worth exporting? List every one in a workspace that
has issues, with counts per repository grouped by project:

```bash
python -m bitbucket_export.workspace_issues workspace --email you@example.com
```

To turn an archive into a browsable website, see
[`static_site`](../static_site/README.md) — a separate tool that reads what this one
writes.

## Archive layout

```text
my-archive/
  manifest.json                  # cross-cutting lookup tables
  issues/
    1.json                       # one file per issue, named by id
    2.json
  assets/
    3f/3fa8c1...e2.png           # every binary, addressed by sha256
    9b/9bd410...77.pdf
  logs/
    migration-20260727-172401.log  # complete log of each run, one file per run
```

Plain JSON and plain files. Nothing about the format depends on this tool still
existing, which is the point of an archive.

One file per issue means you can read, `grep`, or load a single issue without parsing
the whole tracker — which is what analysing an archive usually looks like:

```bash
jq -r .title issues/42.json
grep -l watchdog issues/*.json
```

## The schema

Deliberately **not** Bitbucket's export shape. See [`model.py`](model.py) for the
dataclasses.

`issues/<id>.json` — everything about one issue, self-contained:

| Key | What it holds |
| --- | --- |
| `id`, `title`, `state`, `kind`, `priority` | The issue itself |
| `reporter`, `assignee` | Keys into the manifest's `users` table, not names |
| `created_on`, `updated_on`, `edited_on` | Timestamps, ISO 8601 |
| `milestone`, `component`, `version` | The values this issue uses |
| `votes`, `watches` | Counts. Bitbucket exposes no endpoint listing *who* voted or watched |
| `content.markdown` | The source text exactly as authored, URLs untouched |
| `content.markup` | Which syntax that text is in — `markdown`, `creole`, or `plaintext` |
| `content.image_urls` | Every inline image that text references |
| `comments[]`, `history[]`, `attachments[]` | Nested, so nothing needs joining by id |

A comment with an empty body is normal: Bitbucket attaches a text-less comment to every
field change, paired with the matching `history` entry by id. `comments[].deleted` is
what distinguishes a comment whose text was removed from one that never had any.

`manifest.json` — only what is shared across issues:

| Key | What it holds |
| --- | --- |
| `repository`, `title`, `generated_at`, `schema_version` | Provenance |
| `stats` | Issue, comment, attachment, and asset counts |
| `users` | Interned accounts, so `account_id` and display name both survive — including users known only from `@{account-id}` mentions in issue text |
| `components`, `milestones`, `versions` | Defined on the repository — **including any not used by an issue**, since those are part of the tracker's structure and appear nowhere else |
| `assets` | id → `{path, size, sha256, content_type, filename, origins, source_urls}` |
| `asset_index` | **original Bitbucket URL → asset id** |
| `unresolved_assets` | References whose bytes could not be retrieved, and why |

`asset_index` is the bridge. Markdown keeps its original Bitbucket URLs, so nothing is
lossy; a consumer rewrites a URL by looking it up:

```python
from bitbucket_export import Archive

archive = Archive.load(Path("my-archive"))
for issue in archive.issues:
    for url in issue.content.image_urls:
        local = archive.path_for_url(url)  # "assets/3f/3fa8...e2.png", or None
```

Assets are content-addressed, so the same screenshot pasted into thirty issues is
downloaded once and stored once, regardless of whether it arrived as an attachment or
an inline image.

## What the REST API cannot give you

Worth knowing before you rely on an archive as a complete record:

- **Who** voted or watched. Only counts are exposed; the per-user lists appear in the
  admin export zip but have no API equivalent.
- **Deleted issues.** The API omits them entirely, so a tracker that has had issues
  deleted comes back with gaps in its numbering and no record of what was there. The
  export zip left placeholders; this cannot.
- **`content_updated_on`.** Present in the export zip, absent from the API.

Everything else Bitbucket exposes about an issue is captured. Bitbucket's pre-rendered
`content.html` is deliberately discarded — the source text plus `markup` is the real
record and can be re-rendered, while the HTML cannot be un-rendered.

## What counts as an image

[`markdown_images.py`](markdown_images.py) decides what to download. It deliberately
collects a **superset** of what any one renderer would display — both halves of a
nested `[![a](inner)](outer)`, for instance. Missing an image is unrecoverable once
Bitbucket is gone; a spare one costs kilobytes.

Code spans and fenced blocks are skipped, since URLs in there are illustrative and
would fail to download, masking real failures.

## Re-runs are cheap

The exporter reuses whatever the archive already holds, checking the filesystem rather
than just the manifest — so an interrupted run or a partly deleted archive heals on the
next pass instead of claiming assets it no longer has.

- **Resume an interrupted run** — just run the same command again.
- **Retry only what failed** — same command; stored assets are skipped.
- **Top up with new issues** — same command.
- **Re-fetch one issue** — `--issue-id 42`. Ids named explicitly are always fetched,
  even if unchanged on Bitbucket. Partial runs merge into the archive, so the other
  issues are left alone rather than dropped from the manifest.
- **Rebuild everything but keep the bytes** — add `--refresh`. Because it rebuilds from
  exactly what the run fetches, it refuses to combine with `--issue-id` or
  `--max-issues`.

Unchanged issues are detected by comparing Bitbucket's `updated_on` against the stored
copy, so a re-run costs a few list calls rather than three requests per issue. Writes
go through a temporary file and a rename, so an interrupted run cannot leave a
truncated archive behind.

Because runs merge, an issue deleted on Bitbucket stays in the archive once captured —
usually what you want from an archive. Use `--refresh` for a clean rebuild reflecting
only what Bitbucket currently returns.

## Authentication

Two different credentials, because Bitbucket serves the two kinds of file differently.

**Issues, comments, and attachments** use an Atlassian API token (app passwords are
removed as of 2026-07-28). Create one at **Account settings → Security → API tokens with
scopes**, select **Bitbucket**, and grant `read:issue:bitbucket` and
`read:repository:bitbucket` (`read:account` gives cleaner display names). Supply it via
`--email`/`--token`, `$BITBUCKET_EMAIL`/`$BITBUCKET_API_TOKEN`, or `--token-file`
(defaults to `./BITBUCKET_API_TOKEN`, which is gitignored).

**Inline images** — screenshots pasted into markdown — live on Bitbucket's asset
server, which does not accept API tokens at all. Only a logged-in browser session
works, which is what `--prepare-auth` saves (to `~/.cache/bitbucket-playwright/`).

If that session is missing or expired, the export **stops before fetching anything**
rather than producing an archive that looks complete but silently has no screenshots.
Use `--skip-inline-images` to opt out deliberately; the images are then recorded in
`unresolved_assets` so the gap stays visible.

Checking the session makes one real request, because a stale session is
indistinguishable from a good one on disk — the cookie file still parses and the expiry
dates still look fine, but Bitbucket answers every asset request with a `200` and an
HTML sign-in or two-step-verification page, which would be saved as if it were a
screenshot. Sessions expire in a couple of weeks in practice, so expect to re-run
`--prepare-auth` occasionally; the error says so explicitly when it happens.

## Options

| Flag | Effect |
| --- | --- |
| `--prepare-auth` | Save a browser session, then exit |
| `--skip-inline-images` | Do not fetch inline images; record them as unresolved |
| `--allow-missing-images` | Exit 0 even if some assets failed to download |
| `--refresh` | Rebuild the manifest from scratch, reusing stored assets. Not combinable with `--issue-id` or `--max-issues` |
| `--issue-id N` | Only these issue ids, always re-fetched even if unchanged (repeatable) |
| `--max-issues N` | Cap issue count, lowest ids first |
| `--concurrency N` | Parallel inline-image fetches (default 2) |
| `--request-timeout-ms N` | Per-image timeout (default 120000) |

The exporter exits non-zero if any asset could not be downloaded, so a partial capture
fails a script instead of passing quietly. Re-run to retry just the failures, or pass
`--allow-missing-images` once you have accepted the gaps.

## Writing a consumer

Anything that reads the archive starts the same way:

```python
archive = Archive.load(archive_dir)
archive.path_for_url(url)  # original URL -> stored file
archive.display_name(user_key)  # user key -> display name
archive.issue_ids()  # e.g. for rewriting cross-issue links
```

[`static_site`](../static_site/README.md) is a worked example: it adapts the archive
to its own renderer in about forty lines and depends on this package only for the
schema. Nothing here knows it exists.
