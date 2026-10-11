import time
import uuid
from collections.abc import Iterator

import pytest
import redis

from bookings.locks import LockNotAcquired, RedisLock, get_redis, redis_lock


@pytest.fixture
def lock_key() -> Iterator[str]:
    key = f"deskflow:test:lock:{uuid.uuid4().hex}"
    yield key
    get_redis().delete(key)


@pytest.fixture
def client() -> redis.Redis:
    return get_redis()


def test_second_acquire_fails_while_held(lock_key: str) -> None:
    first = RedisLock(lock_key)
    second = RedisLock(lock_key)

    assert first.acquire()
    assert not second.acquire()


def test_lock_can_be_reacquired_after_release(lock_key: str) -> None:
    first = RedisLock(lock_key)
    assert first.acquire()
    assert first.release()

    assert RedisLock(lock_key).acquire()


def test_release_with_foreign_token_is_noop(lock_key: str, client: redis.Redis) -> None:
    owner = RedisLock(lock_key)
    impostor = RedisLock(lock_key)
    assert owner.acquire()

    assert not impostor.release()
    assert client.get(lock_key) == owner.token


def test_lock_expires_after_ttl(lock_key: str) -> None:
    assert RedisLock(lock_key, ttl_ms=50).acquire()
    time.sleep(0.1)

    assert RedisLock(lock_key).acquire()


def test_expired_owner_cannot_release_new_holder(lock_key: str, client: redis.Redis) -> None:
    stale = RedisLock(lock_key, ttl_ms=50)
    assert stale.acquire()
    time.sleep(0.1)
    fresh = RedisLock(lock_key)
    assert fresh.acquire()

    assert not stale.release()
    assert client.get(lock_key) == fresh.token


def test_context_manager_releases_on_exit(lock_key: str, client: redis.Redis) -> None:
    with redis_lock(lock_key):
        assert client.exists(lock_key)

    assert not client.exists(lock_key)


def test_context_manager_releases_on_error(lock_key: str, client: redis.Redis) -> None:
    with pytest.raises(RuntimeError), redis_lock(lock_key):
        raise RuntimeError("boom")

    assert not client.exists(lock_key)


def test_context_manager_raises_when_contended(lock_key: str) -> None:
    with redis_lock(lock_key), pytest.raises(LockNotAcquired), redis_lock(lock_key):
        pass
