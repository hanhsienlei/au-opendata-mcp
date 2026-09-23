import pytest

from au_opendata_mcp.errors import SqlRejected
from au_opendata_mcp.safety import validate_sql, wrap_with_limit


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t",
        "SELECT suburb, AVG(price) FROM t GROUP BY suburb",
        "SELECT * FROM t WHERE price > 100 ORDER BY price DESC",
        "WITH x AS (SELECT * FROM t) SELECT count(*) FROM x",
        "WITH a AS (SELECT * FROM t), b AS (SELECT * FROM a) SELECT * FROM b",
        "SELECT a FROM t UNION SELECT b FROM t",
        "SELECT strftime(d, '%Y') AS yr, count(*) FROM t GROUP BY 1",
    ],
)
def test_legitimate_read_only_queries_are_accepted(sql):
    assert validate_sql(sql)


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("DROP TABLE t", "only SELECT"),
        ("DELETE FROM t", "only SELECT"),
        ("UPDATE t SET a = 1", "only SELECT"),
        ("INSERT INTO t VALUES (1)", "only SELECT"),
        ("CREATE TABLE z AS SELECT * FROM t", "only SELECT"),
        ("ATTACH '/tmp/x.db' AS x", "only SELECT"),
        ("PRAGMA database_list", "only SELECT"),
        ("INSTALL httpfs", "only SELECT"),
        ("COPY t TO '/tmp/out.csv'", "only SELECT"),
    ],
)
def test_non_select_statements_are_rejected(sql, expected):
    with pytest.raises(SqlRejected, match=expected):
        validate_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t; DROP TABLE t",
        "SELECT * FROM t -- harmless\n; DELETE FROM t",
        "SELECT * FROM t;;",
    ],
)
def test_stacked_statements_are_rejected_including_comment_hidden_ones(sql):
    with pytest.raises(SqlRejected, match="exactly one statement"):
        validate_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv_auto('/etc/passwd')",
        "SELECT * FROM read_parquet('/etc/shadow')",
        "SELECT * FROM glob('/**')",
        "SELECT * FROM other_table",
        "SELECT * FROM t JOIN secrets ON 1=1",
    ],
)
def test_reading_anything_other_than_t_is_rejected(sql):
    with pytest.raises(SqlRejected, match="only the table"):
        validate_sql(sql)


def test_a_denied_function_in_a_projection_is_rejected():
    with pytest.raises(SqlRejected, match="not allowed"):
        validate_sql("SELECT read_text('/etc/passwd') FROM t")


def test_unparseable_sql_is_rejected_with_the_parser_message():
    with pytest.raises(SqlRejected, match="could not parse"):
        validate_sql("SELECT FROM WHERE ***")


def test_empty_sql_is_rejected():
    with pytest.raises(SqlRejected):
        validate_sql("   ")


def test_limit_is_enforced_by_wrapping_not_by_rewriting():
    wrapped = wrap_with_limit("SELECT * FROM t LIMIT 999", 100)
    assert wrapped == "SELECT * FROM (SELECT * FROM t LIMIT 999) LIMIT 101"
