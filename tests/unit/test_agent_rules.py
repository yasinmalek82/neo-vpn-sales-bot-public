"""A path-scoped rule whose glob matches nothing is an inert control."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RULES_DIR = ROOT / "docs" / "agent-rules" / "areas"
AGENTS_MD = ROOT / "AGENTS.md"
RULE_INDEX = RULES_DIR.parent / "README.md"
ENGINEERING_MD = RULES_DIR.parent / "engineering.md"
RULE_FILES = sorted(RULES_DIR.glob("*.md"))

# Keep startup instructions small; detailed guidance is read on demand.
MAX_ROOT_LINES = 100
MAX_ROOT_BYTES = 5000

_NAMED_PATH = re.compile(
    r"`((?:tests|services|bot|scheduler|database|docs)/[\w./-]+\.(?:py|md|json))"
)


def _paths_of(rule: Path) -> list[str]:
    """The `paths:` list of a rule's frontmatter, without a YAML dependency."""
    text = rule.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{rule.name} has no path metadata"
    frontmatter = text.split("---\n", 2)[1]
    return re.findall(r'^\s+-\s+"([^"]+)"\s*$', frontmatter, flags=re.MULTILINE)


def test_there_are_rules_to_check() -> None:
    assert RULE_FILES, "no rule files found; the checks below would pass on nothing"


@pytest.mark.parametrize("rule", RULE_FILES, ids=lambda p: p.name)
def test_every_rule_path_matches_a_file(rule: Path) -> None:
    """A renamed module would otherwise orphan its rule without anyone noticing."""
    patterns = _paths_of(rule)
    assert patterns, f"{rule.name} declares no paths"
    dead = [p for p in patterns if not any(ROOT.glob(p))]
    assert not dead, f"{rule.name}: no file matches {dead}"


@pytest.mark.parametrize(
    "doc", [AGENTS_MD, RULE_INDEX, ENGINEERING_MD, *RULE_FILES], ids=lambda p: p.name
)
def test_every_file_a_rule_names_exists(doc: Path) -> None:
    named = set(_NAMED_PATH.findall(doc.read_text(encoding="utf-8")))
    missing = sorted(p for p in named if not (ROOT / p).exists())
    assert not missing, f"{doc.name} names files that are gone: {missing}"


def test_root_agents_md_stays_short() -> None:
    lines = len(AGENTS_MD.read_text(encoding="utf-8").splitlines())
    assert (
        lines <= MAX_ROOT_LINES
    ), f"AGENTS.md is {lines} lines; move details into docs/agent-rules/"
    size = AGENTS_MD.stat().st_size
    assert size <= MAX_ROOT_BYTES, f"AGENTS.md is {size} bytes; startup budget exceeded"


def test_every_rule_is_indexed() -> None:
    index = RULE_INDEX.read_text(encoding="utf-8")
    for rule in RULE_FILES:
        assert f"(areas/{rule.name})" in index, f"{rule.name} is not discoverable"


def test_legacy_rule_paths_resolve_to_canonical_rules() -> None:
    """Older plans must reach the same rules without maintaining two copies."""
    legacy = ROOT / ".claude" / "rules"
    assert legacy.resolve() == RULES_DIR.resolve()
    for rule in RULE_FILES:
        assert (legacy / rule.name).is_file()
