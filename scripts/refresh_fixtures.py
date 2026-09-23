"""Re-record CKAN fixtures from live endpoints. Run: uv run python scripts/refresh_fixtures.py"""

import json
import pathlib

import httpx

OUT = pathlib.Path(__file__).parent.parent / "tests" / "fixtures" / "ckan"
CALLS = [
    ("data.gov.au", "package_search", {"q": "bus stops", "rows": 5}),
    ("data.gov.au", "package_show", {"id": "national-public-toilet-map"}),
    ("data.gov.au", "organization_list", {"all_fields": True, "limit": 5}),
    ("data.sa.gov.au", "package_search", {"q": "bus stops", "rows": 5}),
    ("data.sa.gov.au", "organization_list", {"all_fields": True, "limit": 5}),
]
BASES = {
    "data.gov.au": "https://data.gov.au/data/api/3/action",
    "data.sa.gov.au": "https://data.sa.gov.au/data/api/3/action",
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for portal, action, params in CALLS:
        r = httpx.get(f"{BASES[portal]}/{action}", params=params, timeout=30.0)
        r.raise_for_status()
        path = OUT / f"{portal}__{action}.json"
        path.write_text(json.dumps(r.json(), indent=2))
        print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
