import asyncio
import contextlib
import tempfile
from pathlib import Path

import httpx
import pytest
import respx

from au_opendata_mcp.cache import BlobCache
from au_opendata_mcp.config import Settings
from au_opendata_mcp.errors import DownloadFailed, ResourceNotQueryable, ResourceTooLarge
from au_opendata_mcp.services.catalog import TABULAR_FORMATS, ResourceInfo
from au_opendata_mcp.services.materialise import TABULAR_READERS, Materialiser

CSV = b"suburb,price\nAdelaide,100\nAdelaide,300\nGlenelg,200\n"
URL = "https://x.org/stops.csv"


def resource(url: str = URL, fmt: str = "CSV", size: int | None = len(CSV)) -> ResourceInfo:
    return ResourceInfo(
        resource_id="r1",
        name="stops.csv",
        format=fmt,
        size_bytes=size,
        url=url,
        datastore_active=False,
        queryable="yes",
        queryable_reason=None,
    )


def build(tmp_path, settings: Settings | None = None) -> Materialiser:
    s = settings or Settings()
    blobs = BlobCache(tmp_path / "blobs", s.materialisation_ttl_seconds, s.cache_max_bytes)
    # Public IPs only in tests; the SSRF resolver is exercised in test_safety_urls.py.
    m = Materialiser(httpx.AsyncClient(follow_redirects=False), s, blobs)
    m._resolve = lambda host: ["93.184.216.34"]
    return m


def test_tabular_readers_matches_catalog_tabular_formats() -> None:
    """TABULAR_READERS (materialise.py) and TABULAR_FORMATS (catalog.py) independently
    enumerate the same formats. They can't be unified into one constant without a circular
    import (materialise.py already imports ResourceInfo from catalog.py), so this guard test
    keeps them from drifting apart silently: a drift would mean get_dataset advertises a
    resource as queryable that materialisation then refuses.
    """
    assert frozenset(TABULAR_READERS) == TABULAR_FORMATS


@respx.mock
async def test_csv_is_downloaded_and_converted_to_parquet(tmp_path):
    respx.get(URL).mock(return_value=httpx.Response(200, content=CSV))
    path = await build(tmp_path).parquet_for(resource(), {URL})
    assert path.suffix == ".parquet"
    assert path.stat().st_size > 0


@respx.mock
async def test_a_second_call_is_served_from_cache_without_another_download(tmp_path):
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=CSV))
    m = build(tmp_path)
    first = await m.parquet_for(resource(), {URL})
    second = await m.parquet_for(resource(), {URL})
    assert first == second
    assert route.call_count == 1


@respx.mock
async def test_a_changed_last_modified_invalidates_the_cache(tmp_path):
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=CSV))
    m = build(tmp_path)
    r1 = resource()
    await m.parquet_for(r1, {URL})
    m_key_old = m.cache_key(r1, "data.gov.au", "2026-01-01")
    m_key_new = m.cache_key(r1, "data.gov.au", "2026-09-01")
    assert m_key_old != m_key_new
    assert route.call_count == 1


@respx.mock
async def test_a_url_not_in_the_catalog_is_refused(tmp_path):
    respx.get("https://evil.org/x.csv").mock(return_value=httpx.Response(200, content=CSV))
    with pytest.raises(DownloadFailed, match="not listed"):
        await build(tmp_path).parquet_for(resource(url="https://evil.org/x.csv"), {URL})


@respx.mock
async def test_streaming_stops_the_moment_the_ceiling_is_crossed(tmp_path):
    settings = Settings(max_resource_bytes=100)
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"y" * 5000))
    with pytest.raises(ResourceTooLarge):
        await build(tmp_path, settings).parquet_for(resource(size=None), {URL})


@respx.mock
async def test_a_lying_content_length_does_not_get_past_the_streaming_cap(tmp_path):
    """content-length is attacker-controlled, so it is a hint and never the limit."""
    settings = Settings(max_resource_bytes=100)
    respx.get(URL).mock(
        return_value=httpx.Response(200, content=b"y" * 5000, headers={"content-length": "10"})
    )
    with pytest.raises(ResourceTooLarge):
        await build(tmp_path, settings).parquet_for(resource(size=None), {URL})


@respx.mock
async def test_an_oversized_download_does_not_leave_a_partial_temp_file_behind(tmp_path):
    """A rejected download must not leak disk space. Repeated oversized requests that
    are each individually refused should not still be able to fill the temp directory.
    """
    import glob
    import tempfile as tempfile_module

    settings = Settings(max_resource_bytes=100)
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"y" * 5000))
    before = set(glob.glob(f"{tempfile_module.gettempdir()}/*.download"))
    with pytest.raises(ResourceTooLarge):
        await build(tmp_path, settings).parquet_for(resource(size=None), {URL})
    after = set(glob.glob(f"{tempfile_module.gettempdir()}/*.download"))
    assert after == before


@respx.mock
async def test_a_redirect_to_a_private_address_is_refused_at_the_hop(tmp_path):
    respx.get(URL).mock(
        return_value=httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})
    )
    m = build(tmp_path)
    m._resolve = lambda host: (
        ["169.254.169.254"] if host == "169.254.169.254" else ["93.184.216.34"]
    )
    with pytest.raises(DownloadFailed, match="non-public"):
        await m.parquet_for(resource(), {URL})


@respx.mock
async def test_a_redirect_loop_is_bounded(tmp_path):
    respx.get(URL).mock(return_value=httpx.Response(302, headers={"location": URL}))
    with pytest.raises(DownloadFailed, match="too many redirects"):
        await build(tmp_path).parquet_for(resource(), {URL})


@respx.mock
async def test_a_non_tabular_format_is_rejected_before_any_download(tmp_path):
    route = respx.get("https://x.org/a.pdf")
    with pytest.raises(ResourceNotQueryable, match="not tabular"):
        await build(tmp_path).parquet_for(
            resource(url="https://x.org/a.pdf", fmt="PDF"), {"https://x.org/a.pdf"}
        )
    assert route.call_count == 0


@respx.mock
async def test_a_malformed_csv_row_is_skipped_rather_than_failing_the_query(tmp_path):
    messy = "name,qty\nCafé,3\nbroken,row,extra\nOk,5\n".encode("latin-1")
    respx.get(URL).mock(return_value=httpx.Response(200, content=messy))
    path = await build(tmp_path).parquet_for(resource(size=None), {URL})
    import duckdb

    rows = duckdb.connect().execute(f"SELECT * FROM read_parquet('{path}')").fetchall()
    assert len(rows) == 2


@respx.mock
async def test_an_unparseable_body_becomes_resource_not_queryable_with_the_link(tmp_path):
    # b"\x00\x01\x02\x03" alone is too tolerant: DuckDB happily reads it as an empty
    # zero-row table rather than raising, which is exactly the "silently becomes a
    # one-column table" failure this test exists to catch (see the brief's Step 4). This
    # body is neither valid UTF-8 nor valid latin-1 (per DuckDB's own encoding check) and
    # so genuinely cannot be coerced into a table under either encoding.
    unparseable = b"\xff\xfe\x00\x01\x80\x81\x82\x83" * 2000
    respx.get(URL).mock(return_value=httpx.Response(200, content=unparseable))
    with pytest.raises(ResourceNotQueryable) as exc:
        await build(tmp_path).parquet_for(resource(size=None), {URL})
    assert exc.value.remedy_url == URL


@respx.mock
async def test_an_upstream_error_on_download_becomes_download_failed(tmp_path):
    respx.get(URL).mock(return_value=httpx.Response(503))
    with pytest.raises(DownloadFailed):
        await build(tmp_path).parquet_for(resource(), {URL})


def _temp_dirs() -> set[Path]:
    root = Path(tempfile.gettempdir())
    return {p for p in root.iterdir() if p.is_dir()}


@respx.mock
async def test_a_successful_conversion_does_not_leave_an_empty_temp_directory_behind(tmp_path):
    """_convert used to reserve its destination with tempfile.mkdtemp() and, on
    success, move only the resulting *file* out via BlobCache.store -- leaving the
    now-empty directory behind on every successful materialisation, forever, since
    nothing ever evicts it (unlike BlobCache's own contents).
    """
    before = _temp_dirs()
    respx.get(URL).mock(return_value=httpx.Response(200, content=CSV))
    await build(tmp_path).parquet_for(resource(), {URL})
    assert _temp_dirs() == before


@respx.mock
async def test_a_failed_conversion_does_not_leave_an_empty_temp_directory_behind(tmp_path):
    before = _temp_dirs()
    unparseable = b"\xff\xfe\x00\x01\x80\x81\x82\x83" * 2000
    respx.get(URL).mock(return_value=httpx.Response(200, content=unparseable))
    with pytest.raises(ResourceNotQueryable):
        await build(tmp_path).parquet_for(resource(size=None), {URL})
    assert _temp_dirs() == before


def test_a_client_that_follows_redirects_is_refused_at_construction(tmp_path):
    """_download's SSRF guard re-validates every redirect hop itself, which only
    works because it sees every hop. A client built with follow_redirects=True
    would follow redirects internally, so that re-validation would never run and the
    guard would be silently bypassed -- this must fail loudly at construction instead.
    """
    settings = Settings()
    blobs = BlobCache(
        tmp_path / "blobs", settings.materialisation_ttl_seconds, settings.cache_max_bytes
    )
    with pytest.raises(ValueError, match="follow_redirects"):
        Materialiser(httpx.AsyncClient(follow_redirects=True), settings, blobs)


@respx.mock
async def test_a_stalled_download_is_stopped_by_the_resource_budget(tmp_path):
    """download_timeout_seconds is httpx's per-phase timeout, not a wall-clock cap --
    a slow-trickle upstream can hold a connection open well past it without ever
    tripping a phase timeout or the byte ceiling. resource_budget_seconds is the
    total per-resource wall-clock cap that closes that gap.
    """

    async def stall(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)
        return httpx.Response(200, content=CSV)

    respx.get(URL).mock(side_effect=stall)
    settings = Settings(resource_budget_seconds=0.05)
    with pytest.raises(DownloadFailed, match="resource budget"):
        await build(tmp_path, settings).parquet_for(resource(), {URL})


# --- fix (I3): _convert must not run on the event loop.
# _convert calls DuckDB (blocking) and _pick_csv_encoding reads and decodes the whole file
# in memory first. Measured before the fix: a 90 MB CSV blocked the loop for 1.09 s and the
# loop got exactly one tick. It also meant the 60 s resource budget in parquet_for could
# never fire during conversion -- asyncio.wait_for has no await point to cancel at.
# This mirrors test_query_execute.py's ticker test for read_parquet_row_count, which is
# offloaded for a footer read of a few bytes.


async def test_the_event_loop_stays_responsive_during_conversion(tmp_path):
    import time

    m = build(tmp_path)
    source = tmp_path / "downloaded.csv"
    source.write_bytes(CSV)

    real_convert = m._convert

    def slow_convert(*args, **kwargs):
        time.sleep(0.3)  # stands in for a large file's decode + DuckDB COPY
        return real_convert(*args, **kwargs)

    m._convert = slow_convert  # type: ignore[method-assign]

    async def fake_download(url: str) -> Path:
        return source

    m._download = fake_download  # type: ignore[method-assign]

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.05)
            ticks += 1

    ticker_task = asyncio.create_task(ticker())
    parquet = await m._fetch_and_convert(URL, "csv")
    ticker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await ticker_task

    assert parquet.exists() and parquet.stat().st_size > 0
    # 0.3s of blocking conversion at 0.05s per tick should allow at least a handful of
    # ticks if the event loop kept running concurrently with the worker thread.
    assert ticks >= 4, f"the event loop was blocked during conversion (only {ticks} ticks)"
