"""Guards for claims the repository makes about itself outside the Python source.

These are cheap, and each one covers a case where the code was right and the surrounding
promise was not -- a CI trigger that never fired on the default branch, a coverage gate that
excluded the security module, and a design-required README section that did not exist.
"""

import pathlib
import re
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"
README = ROOT / "README.md"
PYPROJECT = ROOT / "pyproject.toml"


# --- CI once triggered on push to `main` only, and this repository's default branch
# is `master` -- so a post-merge push ran nothing, while README.md presents CI as the proof
# behind "80% coverage -- a gate, not a claim".


def test_ci_runs_on_a_push_to_either_default_branch_name():
    branches = re.search(r"push:\s*\n\s*branches:\s*\[([^\]]*)\]", CI.read_text())
    assert branches, "the CI workflow no longer has a push branch filter"
    named = {b.strip().strip("\"'") for b in branches.group(1).split(",")}
    assert {"main", "master"} <= named, f"CI would not run on the default branch: {named}"


def test_ci_still_runs_the_coverage_gate():
    assert "--cov-fail-under=80" in CI.read_text()


# --- The coverage gate once covered services/ only, leaving safety.py --
# the SSRF address check and the SQL validator -- outside the one gate the README cites.


def test_the_coverage_gate_includes_the_security_module():
    config = tomllib.loads(PYPROJECT.read_text())
    sources = config["tool"]["coverage"]["run"]["source"]
    assert "au_opendata_mcp.services" in sources
    assert "au_opendata_mcp.safety" in sources


# --- docs/design.md §10 requires three demo scenarios written into the README with
# exact prompts: catalog search, an aggregate query over a real resource, ABN validation.


def test_the_readme_has_the_three_demo_scenarios_with_pasteable_prompts():
    text = README.read_text()
    assert "## Demo scenarios" in text
    section = text.split("## Demo scenarios", 1)[1].split("\n## ", 1)[0]

    # Each scenario's prompt is a markdown blockquote the reader can copy verbatim.
    prompts = [line for line in section.splitlines() if line.startswith("> ")]
    assert len(prompts) >= 3, "each scenario needs an exact, pasteable prompt"

    lowered = section.lower()
    assert "search data.gov.au" in lowered  # 1. catalog search
    assert "sql query" in lowered and "total" in lowered  # 2. an aggregate over a resource
    assert "abn" in lowered  # 3. ABN validation


def test_the_readme_does_not_claim_the_demo_scenarios_were_run():
    section = README.read_text().split("## Demo scenarios", 1)[1].split("\n## ", 1)[0]
    section = " ".join(section.split())  # the source is hard-wrapped
    assert "not been run" in section, (
        "the README's integrity about unrun work must extend to these scenarios"
    )
