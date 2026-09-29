import pytest

from au_opendata_mcp.errors import (
    AuOpenDataError,
    DownloadFailed,
    QueryTimeout,
    ResourceNotFound,
    ResourceNotQueryable,
    ResourceTooLarge,
    SqlRejected,
    UpstreamUnavailable,
)


@pytest.mark.parametrize(
    "cls",
    [
        ResourceNotFound,
        ResourceNotQueryable,
        ResourceTooLarge,
        SqlRejected,
        UpstreamUnavailable,
        DownloadFailed,
        QueryTimeout,
    ],
)
def test_every_domain_error_shares_one_base(cls):
    assert issubclass(cls, AuOpenDataError)


def test_error_carries_a_remedy_url_so_the_caller_is_never_stuck():
    err = ResourceTooLarge(
        "resource is 812 MB, over the 50 MB limit", remedy_url="https://example.org/big.csv"
    )
    assert err.remedy_url == "https://example.org/big.csv"
    assert "812 MB" in str(err)


def test_remedy_url_is_optional():
    assert UpstreamUnavailable("data.gov.au timed out").remedy_url is None
