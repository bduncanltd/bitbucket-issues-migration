"""Generate a static HTML site from a Bitbucket issue archive.

One consumer of the archive schema, not a privileged part of it. It depends on
``bitbucket_export`` only to read the schema; nothing in the exporter knows this
package exists.
"""

from .site import render_site

__all__ = ["render_site"]
