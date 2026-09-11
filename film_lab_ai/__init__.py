# SPDX-License-Identifier: GPL-3.0-only
"""Optional, on-device photo indexing for Film Lab.

The package deliberately has no imports from the render pipeline.  The server
provides the few callbacks it needs, so disabling or removing this directory
does not change photo rendering or editing.
"""

from .service import AIIndexService

__all__ = ["AIIndexService"]
