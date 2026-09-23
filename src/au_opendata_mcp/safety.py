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
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise DownloadFailed(
                f"refusing to fetch {host!r}: it resolves to the non-public address {ip}"
            )


def assert_url_in_catalog(url: str, catalog_urls: Collection[str]) -> None:
    """Only URLs the portal itself published for this resource may be fetched."""
    if url not in set(catalog_urls):
        raise DownloadFailed(
            "refusing to fetch a URL that is not listed in the dataset's catalog metadata"
        )
