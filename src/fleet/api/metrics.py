"""Back-compat shim. Metrics now live in fleet.observability.metrics so they can
be emitted from the shared instrumentation point (traced_node), not just the API."""
from ..observability.metrics import *  # noqa: F401,F403
from ..observability.metrics import CONTENT_TYPE, render  # noqa: F401
