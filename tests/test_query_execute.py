import datetime
import decimal
from pathlib import Path

import duckdb
import pytest

from au_opendata_mcp.errors import SqlRejected
from au_opendata_mcp.services.query import execute_in_sandbox, jsonable


@pytest.fixture
def parquet(tmp_path) -> Path:
    csv = tmp_path / "s.csv"
    csv.write_text("suburb,price\nAdelaide,100\nAdelaide,300\nGlenelg,200\n")
    out = tmp_path / "s.parquet"
    duckdb.connect().execute(
        f"COPY (SELECT * FROM read_csv_auto('{csv}')) TO '{out}' (FORMAT PARQUET)"
    )
    return out


def test_a_normal_aggregate_query_works(parquet):
    columns, rows, truncated = execute_in_sandbox(
        parquet,
        "SELECT suburb, avg(price) AS mean FROM t GROUP BY suburb ORDER BY suburb",
        limit=100,
        memory_limit="1GB",
    )
    assert [c.name for c in columns] == ["suburb", "mean"]
    assert rows == [{"suburb": "Adelaide", "mean": 200.0}, {"suburb": "Glenelg", "mean": 200.0}]
    assert truncated is False


def test_the_sandbox_keeps_t_readable_and_the_filesystem_closed(parquet):
    """Spec section 6.2: both properties must hold on the same connection. This test is the
    reason the two SET statements come after the CREATE TABLE, and must never be relaxed."""
    _, rows, _ = execute_in_sandbox(parquet, "SELECT count(*) AS n FROM t", 100, "1GB")
    assert rows == [{"n": 3}]

    for hostile in [
        "SELECT * FROM read_csv_auto('/etc/passwd')",
        f"SELECT * FROM read_parquet('{parquet}')",
        "SELECT * FROM glob('/**')",
    ]:
        with pytest.raises(SqlRejected):
            execute_in_sandbox(parquet, hostile, 100, "1GB")


def test_the_lockdown_cannot_be_reversed_by_caller_sql(parquet):
    with pytest.raises(SqlRejected):
        execute_in_sandbox(parquet, "SET enable_external_access = true", 100, "1GB")


def test_truncation_is_reported_without_returning_the_extra_row(parquet):
    columns, rows, truncated = execute_in_sandbox(
        parquet, "SELECT * FROM t", limit=2, memory_limit="1GB"
    )
    assert len(rows) == 2
    assert truncated is True


def test_a_result_at_exactly_the_limit_is_not_reported_as_truncated(parquet):
    _, rows, truncated = execute_in_sandbox(parquet, "SELECT * FROM t", limit=3, memory_limit="1GB")
    assert len(rows) == 3
    assert truncated is False


def test_a_runtime_sql_error_becomes_sql_rejected_with_the_engine_message(parquet):
    with pytest.raises(SqlRejected, match="no_such_column|not found|Binder"):
        execute_in_sandbox(parquet, "SELECT no_such_column FROM t", 100, "1GB")


# --- jsonable: rows must survive JSON serialisation for both MCP and REST ---


def test_dates_and_times_become_iso_strings():
    assert jsonable(datetime.date(2026, 7, 1)) == "2026-07-01"
    assert jsonable(datetime.datetime(2026, 7, 1, 9, 30)) == "2026-07-01T09:30:00"


def test_decimals_become_floats():
    assert jsonable(decimal.Decimal("12.50")) == 12.5


def test_bytes_become_hex():
    assert jsonable(b"\x01\x02") == "0102"


def test_ordinary_values_pass_through():
    assert jsonable("Adelaide") == "Adelaide"
    assert jsonable(None) is None
    assert jsonable(42) == 42


def test_rows_containing_dates_are_serialisable(tmp_path):
    csv = tmp_path / "d.csv"
    csv.write_text("when,n\n2026-07-01,1\n")
    out = tmp_path / "d.parquet"
    duckdb.connect().execute(
        f"COPY (SELECT * FROM read_csv_auto('{csv}')) TO '{out}' (FORMAT PARQUET)"
    )
    import json

    _, rows, _ = execute_in_sandbox(out, "SELECT * FROM t", 100, "1GB")
    json.dumps(rows)  # must not raise
