# Archive Site

Turns a Bitbucket issue archive into a static HTML site.

```bash
python -m static_site .archive/workspace/repo
```

The site lands in `.site/<workspace>/<repo>`, mirroring the archive layout; pass a
second argument to write somewhere else.

Reads the archive and nothing else — no network, no credentials. Regenerating after a
template or CSS change takes seconds and can be repeated as often as you like, because
the archive is the source of truth and the site is disposable output.

Create the archive first with
[`bitbucket_export`](../bitbucket_export/README.md).

## Output

```text
my-site/
  index.html          # searchable, sortable issue table
  1.html, 2.html ...  # one page per issue
  assets/
    style.css
    3f/3fa8c1...e2.png
```

Self-contained and relative-linked, so it works opened from the filesystem or published
to GitHub Pages or any static host.

Assets are hardlinked from the archive where the filesystem allows it, so generating a
site next to a multi-gigabyte archive does not double the disk usage. It falls back to
copying across volumes.

## How it uses the archive

Everything goes through the published schema — there is no private channel back to the
exporter:

```python
archive.path_for_url(url)  # rewrite a markdown URL to a local file
archive.display_name(user_key)  # resolve an interned user
archive.issue_ids()  # rewrite cross-issue Bitbucket links to local pages
```

`ArchiveLocalizer` in [`site.py`](site.py) is that logic, and the adapters below it
convert archive dataclasses into the flat dicts [`rendering.py`](rendering.py) expects.
If you write a different consumer — a Jira importer, a search index — it would start
from the same three calls.

## Images the archive does not have

If Bitbucket could not serve an image when the archive was captured, it is listed in
the manifest's `unresolved_assets` and the page renders without it rather than failing.
The count is reported at the end of a run.

A different situation is an asset the manifest *does* list whose file is missing from
the archive — that means the archive is damaged or incomplete, so the generator stops
before writing anything rather than silently publishing broken links. Pass
`--allow-missing-assets` to accept the gaps deliberately.

In either case the fix is the same: re-run the exporter — it retries only what is
missing — then regenerate the site.
