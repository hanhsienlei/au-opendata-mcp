import pytest

from au_opendata_mcp.portals import PORTALS, all_portals, get_portal


def test_both_portals_registered():
    assert set(PORTALS) == {"data.gov.au", "data.sa.gov.au"}


def test_portal_api_bases_are_verified_values():
    assert get_portal("data.gov.au").api_base == "https://data.gov.au/data/api/3/action"
    assert get_portal("data.sa.gov.au").api_base == "https://data.sa.gov.au/data/api/3/action"


def test_unknown_portal_raises_with_the_valid_options_named():
    with pytest.raises(ValueError) as exc:
        get_portal("data.nsw.gov.au")
    assert "data.gov.au" in str(exc.value)


def test_all_portals_returns_every_registered_portal():
    assert len(all_portals()) == 2
