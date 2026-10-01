"""The router's deterministic gate. Nothing replays unless it accepts the proposal: real capability,
the only one needed, exact inputs, right types, values grounded in the goal, one argument per
mention."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from artifact.grounding import count_occurrences, first_index, is_grounded, match_option, normalize
from router.catalog import CatalogEntry

# Words that point back at something already named ("its checking"), so one mention can serve two arguments.
_BACK_REFERENCE = re.compile(r"\b(?:its|their|his|her|same|own|within)\b")

_NOT_A_VALUE = {"", "null", "none", "n/a", "unknown", "unspecified", "not specified"}


@dataclass
class Validation:
    ok: bool
    reason: str
    capability_id: str | None = None
    params: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)  # a value read as a near-miss spelling, shown to the person


def _reject(reason: str) -> Validation:
    return Validation(ok=False, reason=reason)


def _mentions(option: str, goal: str) -> tuple[int, str]:
    """How many times the request names this dropdown choice, allowing a close misspelling, and the wording
    it used the first time. Whole words only, non-overlapping."""
    words = re.findall(r"[a-z0-9]+", normalize(goal))
    want = re.findall(r"[a-z0-9]+", normalize(option))
    n, count, first, i = len(want), 0, "", 0
    while n and i + n <= len(words):
        window = words[i : i + n]
        if difflib.SequenceMatcher(None, " ".join(window), " ".join(want)).ratio() >= 0.8:
            count, first, i = count + 1, first or " ".join(window), i + n
        else:
            i += 1
    return count, first


def _referred_back(value: str, names: list[str], declared: dict, goal: str) -> bool:
    """One mention may serve two arguments only when a back-reference follows it ("from 12345 savings to its
    checking") and the repeated inputs are numbers (identifiers). A repeated word like "checking" is exactly
    the borrowing this rule exists to stop."""
    if any(declared[n].type != "number" for n in names):
        return False
    start = first_index(value, goal)
    return start >= 0 and _BACK_REFERENCE.search(normalize(goal)[start + len(normalize(value)):]) is not None


def validate_proposal(goal: str, proposal: dict, catalog: dict[str, CatalogEntry]) -> Validation:
    capability_id = proposal.get("capability_id")
    if capability_id in (None, "", "none"):
        return _reject("the model found no saved capability that matches this request")
    entry = catalog.get(capability_id)
    if entry is None:
        return _reject(f"the model named {capability_id!r}, which is not a saved capability")
    needed = proposal.get("needed_capabilities")
    if not isinstance(needed, list) or set(needed) != {capability_id}:
        return _reject(f"the request needs {needed if isinstance(needed, list) else 'an unknown set of'} capabilities, and this one does exactly {capability_id!r}")
    if proposal.get("covers_whole_request") is not True:
        return _reject(f"{capability_id!r} does not cover the whole request (it asks for more than one operation, or more than it does)")

    args = proposal.get("args") or {}
    declared = {i.name: i for i in entry.inputs}
    unknown = sorted(set(args) - set(declared))
    if unknown:
        return _reject(f"the model invented input(s) {unknown} that {capability_id!r} does not take")
    missing = sorted(i.name for i in entry.inputs if i.required and i.name not in args)
    if missing:
        return _reject(f"input(s) {missing} were not stated in the request")

    params: dict[str, str] = {}
    claims: dict[str, tuple[str, int]] = {}  # input name -> (what it claims, how many times the request says it)
    notes: list[str] = []
    for name, raw in args.items():
        value = str(raw).strip()
        if value.lower() in _NOT_A_VALUE:
            return _reject(f"input {name!r} was not stated in the request")
        choices = declared[name].allowed_values
        if declared[name].type == "string" and choices:
            option, kind = match_option(value, choices)
            if option is None:
                return _reject(f"input {name!r} = {value!r} is not one of the choices {choices}")
            said, wording = _mentions(option, goal)
            if said == 0:
                return _reject(f"input {name!r} = {value!r} does not appear in the request")
            if normalize(wording) != normalize(option):
                notes.append(f"read {wording!r} as {option!r} for {name}")
            params[name], claims[name] = option, (normalize(option), said)  # the page's own spelling
            continue
        if declared[name].type == "number":
            value = re.sub(r"[$,%\s]", "", value)
            try:
                float(value)
            except ValueError:
                return _reject(f"input {name!r} is {raw!r}, which is not a number")
        if not is_grounded(value, goal):
            return _reject(f"input {name!r} = {value!r} does not appear in the request")
        example = declared[name].example
        if declared[name].type == "string" and example and normalize(value) == normalize(example):
            value = example  # no choice list recorded: still use the artifact's own spelling ("Auto", not "auto")
        params[name], claims[name] = value, (normalize(value), count_occurrences(value, goal))

    by_value: dict[str, list[str]] = {}
    for name, (key, _said) in claims.items():
        by_value.setdefault(key, []).append(name)
    for key, names in by_value.items():
        said = claims[names[0]][1]
        if len(names) <= said:
            continue
        if len(names) - said == 1 and said >= 1 and _referred_back(params[names[0]], names, declared, goal):
            continue
        return _reject(f"inputs {sorted(names)} all take {params[names[0]]!r}, but the request says it fewer times than that")

    return Validation(ok=True, reason="validated", capability_id=capability_id, params=params, notes=notes)
