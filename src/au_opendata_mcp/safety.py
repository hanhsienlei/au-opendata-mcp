import ipaddress
import socket
from collections.abc import Callable, Collection
from urllib.parse import urlparse

from au_opendata_mcp.errors import DownloadFailed

SAFE_SCHEMES = frozenset({"http", "https"})
MAX_REDIRECTS = 5


def _resolve_host(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def assert_safe_url(url: str, *, resolve: Callable[[str], list[str]] = _resolve_host) -> None:
    """Refuse anything that is not a public http(s) address.

    Every resolved address is checked, not just the first, so a host that answers with
    one public and one private address cannot slip through.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in SAFE_SCHEMES:
        raise DownloadFailed(
            f"refusing {scheme or 'scheme-less'} URL; only http and https are allowed"
        )

    host = parsed.hostname
    if not host:
        raise DownloadFailed(f"URL has no host: {url!r}")

    try:
        addresses = resolve(host)
    except OSError as exc:
        raise DownloadFailed(f"could not resolve host {host!r}: {exc}") from exc

    if not addresses:
        raise DownloadFailed(f"host {host!r} resolved to no addresses")

    for raw in addresses:
        ip = ipaddress.ip_address(raw)
        # `is_global` is the basis rather than enumerating private/loopback/link-local/
        # reserved/unspecified by hand: hand-enumerated ranges are exactly what let
        # CGNAT space (100.64.0.0/10) through undetected, and new reserved ranges get
        # allocated over time. `is_multicast` must still be checked separately and
        # surprisingly: `ip.is_global` is True for multicast addresses (e.g.
        # 224.0.0.1), so `not is_global` alone would let multicast through.
        if not ip.is_global or ip.is_multicast:
            raise DownloadFailed(
                f"refusing to fetch {host!r}: it resolves to the non-public address {ip}"
            )


def assert_url_in_catalog(url: str, catalog_urls: Collection[str]) -> None:
    """Only URLs the portal itself published for this resource may be fetched."""
    if url not in set(catalog_urls):
        raise DownloadFailed(
            "refusing to fetch a URL that is not listed in the dataset's catalog metadata"
        )
