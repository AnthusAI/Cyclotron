"""Provider-neutral exact-request cache controls for decision collection."""
from dataclasses import dataclass


class CacheMiss(RuntimeError):
    """Cache-only collection cannot satisfy this exact request."""


@dataclass(frozen=True)
class CacheOptions:
    policy: str = 'reuse'
    retry_failed: bool = False

    def __post_init__(self):
        if self.policy not in {'reuse', 'cache_only', 'refresh'}:
            raise ValueError('unknown decision cache policy')
        if type(self.retry_failed) is not bool:
            raise ValueError('retry_failed must be boolean')
