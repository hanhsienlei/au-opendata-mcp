import pytest

from au_opendata_mcp.errors import DownloadFailed
from au_opendata_mcp.safety import assert_safe_url, assert_url_in_catalog


def fake_resolver(mapping: dict[str, list[str]]):
    def resolve(host: str) -> list[str]:
        if host not in mapping:
            raise OSError(f"no such host {host}")
        return mapping[host]

    return resolve


PUBLIC = fake_resolver({"data.gov.au": ["104.18.1.1"], "x.org": ["93.184.216.34"]})


def test_a_public_https_url_is_allowed():
    assert assert_safe_url("https://data.gov.au/data.csv", resolve=PUBLIC) is None


def test_a_public_ipv6_url_is_allowed():
    resolve = fake_resolver({"v6.example.org": ["2606:2800:220:1::"]})
    assert assert_safe_url("https://v6.example.org/data.csv", resolve=resolve) is None


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://x.org/a.csv",
        "gopher://x.org/a",
        "jar:http://x.org/a!/b",
    ],
)
def test_non_http_schemes_are_refused(url):
    with pytest.raises(DownloadFailed, match="http and https"):
        assert_safe_url(url, resolve=PUBLIC)


@pytest.mark.parametrize(
    "host,ip,label",
    [
        ("localhost", "127.0.0.1", "loopback"),
        ("metadata", "169.254.169.254", "cloud metadata service"),
        ("internal", "10.0.0.1", "private class A"),
        ("internal", "192.168.1.1", "private class C"),
        ("internal", "172.16.0.1", "private class B"),
        ("six", "::1", "IPv6 loopback"),
        ("sixlocal", "fd00::1", "IPv6 unique local"),
        ("cgnat", "100.64.0.1", "carrier-grade NAT / shared address space"),
        ("mcast", "224.0.0.1", "multicast"),
        ("testnet", "192.0.2.1", "TEST-NET-1"),
        ("benchmark", "198.18.0.1", "benchmarking range"),
        ("unspec", "0.0.0.0", "unspecified"),
    ],
)
def test_hosts_resolving_to_non_public_addresses_are_refused(host, ip, label):
    with pytest.raises(DownloadFailed, match="non-public"):
        assert_safe_url(f"http://{host}/a.csv", resolve=fake_resolver({host: [ip]}))


def test_a_host_with_one_public_and_one_private_address_is_refused():
    """DNS rebinding defence: every resolved address must be public, not just the first."""
    resolve = fake_resolver({"sneaky.org": ["93.184.216.34", "127.0.0.1"]})
    with pytest.raises(DownloadFailed, match="non-public"):
        assert_safe_url("http://sneaky.org/a.csv", resolve=resolve)


def test_unresolvable_host_is_refused_not_attempted():
    with pytest.raises(DownloadFailed, match="could not resolve"):
        assert_safe_url("http://nope.invalid/a.csv", resolve=PUBLIC)


def test_url_with_no_host_is_refused():
    with pytest.raises(DownloadFailed, match="no host"):
        assert_safe_url("http:///a.csv", resolve=PUBLIC)


def test_catalog_membership_is_required():
    catalog = {"https://x.org/a.csv"}
    assert assert_url_in_catalog("https://x.org/a.csv", catalog) is None
    with pytest.raises(DownloadFailed, match="not listed"):
        assert_url_in_catalog("https://x.org/evil.csv", catalog)
