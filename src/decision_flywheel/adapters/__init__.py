"""Optional adapters for concrete third-party decision models.

Jev is TypeSafe AI's hosted System One model, Kev is a decision model run as a
local server exposing the same System One API, and Laya is Convai Innovations'
open-weight ``convaiinnovations/laya`` model loaded in-process.
``SystemOneAdapter`` accepts any compatible System One client.
"""

from .kev import KevAdapter
from .laya import LayaAdapter
from .system_one import SystemOneAdapter

__all__ = ["KevAdapter", "LayaAdapter", "SystemOneAdapter"]
