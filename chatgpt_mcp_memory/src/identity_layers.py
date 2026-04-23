"""ISA layer model: seven claim layers, field vocabulary, access tiers."""
from __future__ import annotations

from typing import Dict, FrozenSet, Optional, Sequence, Set, Tuple

MIN_LAYER = 1
MAX_LAYER = 7

# Open: any agent with session permission. Selective: user must grant layer in-session.
# Locked: same as selective for this codebase (UI prompt-time unlock simulated via grants header).
OPEN_LAYERS: FrozenSet[int] = frozenset({1, 3, 6})
SELECTIVE_LAYERS: FrozenSet[int] = frozenset({2, 4, 5})
LOCKED_LAYERS: FrozenSet[int] = frozenset({7})

LAYER_ACCESS_TIER: Dict[int, str] = {
    1: "open",
    2: "selective",
    3: "open",
    4: "selective",
    5: "selective",
    6: "open",
    7: "locked",
}

LAYER_TITLES: Dict[int, str] = {
    1: "Identity facts",
    2: "Beliefs and values",
    3: "Goals and intentions",
    4: "Relationships",
    5: "Behavioral patterns",
    6: "Preferences",
    7: "Sensitive attributes",
}

LAYER_FIELDS: Dict[int, FrozenSet[str]] = {
    1: frozenset(
        {
            "full_name",
            "preferred_name",
            "date_of_birth",
            "nationality",
            "primary_language",
            "secondary_languages",
            "current_city",
            "home_ownership_status",
            "occupation_title",
            "employer_or_venture_name",
        }
    ),
    2: frozenset(
        {
            "religious_tradition",
            "theological_commitments",
            "political_orientation_broad",
            "ethical_non_negotiables",
            "priority_hierarchy",
            "stated_worldview",
        }
    ),
    3: frozenset(
        {
            "active_project",
            "financial_target",
            "life_milestone_goal",
            "named_blocker",
            "time_sensitivity_flag",
        }
    ),
    4: frozenset(
        {
            "family_member",
            "close_personal_contact",
            "professional_relationship",
            "organization_affiliation",
        }
    ),
    5: frozenset(
        {
            "work_rhythm",
            "decision_making_style",
            "risk_tolerance",
            "communication_preference",
            "spending_behavior_class",
        }
    ),
    6: frozenset(
        {
            "aesthetic_preference",
            "cuisine",
            "music",
            "reading_interest",
            "geographic_preference",
            "communication_tone",
            "tool_software_preference",
        }
    ),
    7: frozenset(
        {
            "health_condition",
            "financial_position",
            "legal_status",
            "trauma_history",
            "sexual_orientation",
            "family_conflict_history",
        }
    ),
}

# Legacy `kind` on identity_claims → default layer when `layer` column is null.
KIND_DEFAULT_LAYER: Dict[str, int] = {
    "fact": 1,
    "value": 2,
    "boundary": 2,
    "goal": 3,
    "relationship": 4,
    "pattern": 5,
    "preference": 6,
    "sensitive": 7,
}


def effective_layer(row: Dict[str, object]) -> int:
    """Resolve ISA layer for a claim row (dict from store)."""
    raw = row.get("layer")
    if raw is not None:
        try:
            v = int(raw)
            if MIN_LAYER <= v <= MAX_LAYER:
                return v
        except (TypeError, ValueError):
            pass
    kind = str(row.get("kind") or "").strip().lower()
    return KIND_DEFAULT_LAYER.get(kind, 3)


def normalize_layers(requested: Sequence[int]) -> Tuple[Optional[str], Set[int]]:
    out: Set[int] = set()
    for x in requested:
        try:
            v = int(x)
        except (TypeError, ValueError):
            return f"invalid layer: {x!r}", set()
        if v < MIN_LAYER or v > MAX_LAYER:
            return f"layer out of range: {v}", set()
        out.add(v)
    return None, out


def parse_session_grants(header_value: Optional[str]) -> Set[int]:
    if not header_value or not str(header_value).strip():
        return set()
    out: Set[int] = set()
    for part in str(header_value).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            v = int(part)
        except ValueError:
            continue
        if MIN_LAYER <= v <= MAX_LAYER:
            out.add(v)
    return out


def resolve_granted_layers(
    requested: Set[int], session_grants: Set[int]
) -> Tuple[Set[int], Set[int]]:
    """Return (granted, denied) where denied = requested - granted."""
    granted: Set[int] = set()
    for L in requested:
        if L in OPEN_LAYERS:
            granted.add(L)
        elif L in SELECTIVE_LAYERS or L in LOCKED_LAYERS:
            if L in session_grants:
                granted.add(L)
    denied = set(requested) - granted
    return granted, denied


def validate_field_for_layer(layer: int, field: Optional[str]) -> Optional[str]:
    if field is None or not str(field).strip():
        return None
    f = str(field).strip().lower()
    allowed = LAYER_FIELDS.get(layer)
    if allowed is None:
        return "invalid layer"
    if f not in allowed:
        return f"field {f!r} not valid for layer {layer}"
    return None
