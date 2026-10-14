import copy
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from ai_router.routing.policy import RoutingPolicy, load_policy
from tests.conftest import POLICY

TASK_ADDRESSES = {
    "human-resources@example.com",
    "help-desk@example.com",
    "it@example.com",
    "kadry@example.com",
    "other@example.com",
}


def raw_policy() -> dict[str, Any]:
    return copy.deepcopy(POLICY.spec.model_dump(mode="json"))


def test_default_policy_matches_the_task() -> None:
    assert set(POLICY.addresses) == TASK_ADDRESSES
    assert POLICY.fallback.address == "other@example.com"
    assert all(d.examples for d in POLICY.departments)


def test_policy_from_file_override(tmp_path) -> None:  # type: ignore[no-untyped-def]
    data = raw_policy()
    data["version"] = "custom-1"
    data["departments"][0]["address"] = "people@firma.pl"
    data["decision_rules"] = []
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    policy = load_policy(str(path))
    assert policy.version == "custom-1"
    assert "people@firma.pl" in policy.addresses
    assert policy.source == str(path)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["departments"].append(dict(d["departments"][0])),  # duplicate key/address
        lambda d: d["departments"][0].update(fallback=True),  # two fallbacks
        lambda d: d["decision_rules"].append(
            "Everything -> nobody@example.com."
        ),  # unknown address
        lambda d: d["departments"][0].update(address="not-an-email"),
        lambda d: d["departments"][0].update(key="Bad Key"),
        lambda d: d.update(departments=d["departments"][:1]),  # fewer than two departments
    ],
    ids=["duplicate", "two-fallbacks", "unknown-rule-address", "bad-address", "bad-key", "too-few"],
)
def test_invalid_policies_fail_fast(mutate) -> None:  # type: ignore[no-untyped-def]
    data = raw_policy()
    mutate(data)
    with pytest.raises(ValidationError):
        RoutingPolicy.from_mapping(data)


def test_json_schema_is_a_flat_enum() -> None:
    assert POLICY.json_schema() == {"type": "string", "enum": list(POLICY.addresses)}


def test_normalize_is_lenient() -> None:
    assert POLICY.normalize(" HELP-DESK@EXAMPLE.COM ") == "help-desk@example.com"
    assert POLICY.normalize("help_desk") == "help-desk@example.com"
    assert POLICY.normalize("human-resources") == "human-resources@example.com"
    assert POLICY.normalize("kadry") == "kadry@example.com"
    assert POLICY.normalize("support@example.com") == "support@example.com"
    assert POLICY.normalize(42) == 42


def test_validate_address_lists_allowed_values() -> None:
    assert POLICY.validate_address("IT@example.com") == "it@example.com"
    with pytest.raises(ValueError, match=r"help-desk@example\.com"):
        POLICY.validate_address("support@example.com")


def test_digest_is_stable_and_content_sensitive() -> None:
    data = raw_policy()
    assert RoutingPolicy.from_mapping(data).digest == POLICY.digest
    data["departments"][0]["scope"] += " (changed)"
    assert RoutingPolicy.from_mapping(data).digest != POLICY.digest
