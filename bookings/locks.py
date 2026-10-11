"""Short-lived Redis locks that serialise concurrent hold attempts on the same resource.

The lock is an optimisation, not the source of truth: it keeps two checkout requests for the
same slot from racing each other into the database, so the loser gets a fast, friendly
"slot unavailable" instead of an IntegrityError. The exclusion constraint on ``Booking``
remains the final guarantee if Redis is unavailable or a lock expires mid-transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache

import redis
from django.conf import settings

DEFAULT_TTL_MS = 10_000

# Delete the key only if it still holds our token, so a client whose lock already expired
# can never release a lock that has since been acquired by someone else.
_RELEASE_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
end
return 0
"""


class LockNotAcquired(Exception):
    """Another worker currently holds the lock."""


@cache
def get_redis() -> redis.Redis:
    return redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)


def resource_lock_key(resource_id: int) -> str:
    return f"deskflow:lock:resource:{resource_id}"


class RedisLock:
    def __init__(
        self, key: str, ttl_ms: int = DEFAULT_TTL_MS, client: redis.Redis | None = None
    ) -> None:
        self.key = key
        self.ttl_ms = ttl_ms
        self.client = client or get_redis()
        self.token = uuid.uuid4().hex

    def acquire(self) -> bool:
        # SET NX PX is atomic: create-if-absent and set expiry in one round trip.
        return bool(self.client.set(self.key, self.token, nx=True, px=self.ttl_ms))

    def release(self) -> bool:
        return bool(self.client.eval(_RELEASE_SCRIPT, 1, self.key, self.token))


@contextmanager
def redis_lock(
    key: str, ttl_ms: int = DEFAULT_TTL_MS, client: redis.Redis | None = None
) -> Iterator[RedisLock]:
    lock = RedisLock(key, ttl_ms=ttl_ms, client=client)
    if not lock.acquire():
        raise LockNotAcquired(key)
    try:
        yield lock
    finally:
        lock.release()
