"""Export a Bitbucket issue tracker into a reusable, self-contained archive.

One pass fetches issues, comments, history, attachments, and inline images into an
archive directory: ``manifest.json`` plus content-addressed ``assets/``. That archive
is the deliverable, and this package stops there — see :mod:`bitbucket_export.model`
for the schema consumers read.
"""

from .model import SCHEMA_VERSION, Archive

__all__ = ["SCHEMA_VERSION", "Archive"]
