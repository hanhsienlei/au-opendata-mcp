import os
from pathlib import Path

from au_opendata_mcp.cache import BlobCache, MetadataCache


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- MetadataCache ---


def test_set_then_get_returns_the_value():
    c = MetadataCache(max_entries=10, ttl_seconds=60, clock=FakeClock())
    c.set("k", {"a": 1})
    assert c.get("k") == {"a": 1}


def test_missing_key_returns_none():
    assert MetadataCache(10, 60, clock=FakeClock()).get("nope") is None


def test_entry_expires_after_its_ttl():
    clock = FakeClock()
    c = MetadataCache(10, 60, clock=clock)
    c.set("k", "v")
    clock.advance(59)
    assert c.get("k") == "v"
    clock.advance(2)
    assert c.get("k") is None


def test_least_recently_used_entry_is_evicted_when_full():
    c = MetadataCache(max_entries=2, ttl_seconds=600, clock=FakeClock())
    c.set("a", 1)
    c.set("b", 2)
    c.get("a")  # 'a' is now the most recently used
    c.set("c", 3)  # evicts 'b'
    assert c.get("a") == 1
    assert c.get("b") is None
    assert c.get("c") == 3


def test_a_cache_hit_does_not_extend_freshness():
    clock = FakeClock()
    c = MetadataCache(10, 60, clock=clock)
    c.set("k", "v")
    clock.advance(40)
    assert c.get("k") == "v"  # a hit
    clock.advance(30)  # 70s since write, still only 60s TTL
    assert c.get("k") is None


# --- BlobCache ---


def write_blob(tmp_path: Path, name: str, size: int) -> Path:
    p = tmp_path / name
    p.write_bytes(b"x" * size)
    return p


def test_store_then_path_for_returns_the_cached_file(tmp_path):
    cache = BlobCache(tmp_path / "cache", ttl_seconds=3600, max_bytes=10_000, clock=FakeClock())
    stored = cache.store("key1", write_blob(tmp_path, "a.parquet", 100))
    assert stored.exists()
    assert cache.path_for("key1") == stored
    assert stored.read_bytes() == b"x" * 100


def test_path_for_an_unknown_key_is_none(tmp_path):
    cache = BlobCache(tmp_path / "cache", 3600, 10_000, clock=FakeClock())
    assert cache.path_for("nope") is None


def test_expired_blob_is_removed_and_reported_as_a_miss(tmp_path):
    clock = FakeClock()
    cache = BlobCache(tmp_path / "cache", ttl_seconds=100, max_bytes=10_000, clock=clock)
    stored = cache.store("k", write_blob(tmp_path, "a.parquet", 50))
    clock.advance(101)
    assert cache.path_for("k") is None
    assert not stored.exists()


def test_disk_budget_evicts_least_recently_accessed_first(tmp_path):
    clock = FakeClock()
    cache = BlobCache(tmp_path / "cache", ttl_seconds=3600, max_bytes=250, clock=clock)
    a = cache.store("a", write_blob(tmp_path, "a.parquet", 100))
    b = cache.store("b", write_blob(tmp_path, "b.parquet", 100))
    # Make 'a' look older by access time than 'b'.
    os.utime(a, (clock.now - 500, a.stat().st_mtime))
    os.utime(b, (clock.now - 10, b.stat().st_mtime))
    cache.store("c", write_blob(tmp_path, "c.parquet", 100))  # total 300 > 250
    assert cache.total_bytes() <= 250
    assert not a.exists()
    assert b.exists()


def test_different_keys_do_not_collide(tmp_path):
    cache = BlobCache(tmp_path / "cache", 3600, 10_000, clock=FakeClock())
    cache.store("k1", write_blob(tmp_path, "one.parquet", 10))
    cache.store("k2", write_blob(tmp_path, "two.parquet", 20))
    assert cache.path_for("k1") != cache.path_for("k2")
    assert cache.path_for("k1").stat().st_size == 10
