"""The Task AI entry point's routing decision: goal + target -> replay an
existing capability, or fall back to discovery.

This module is deliberately NOT a computer-use agent. It never touches
Playwright, never opens a page, never reasons about individual UI elements,
and never calls an LLM (verified by tests/test_router.py's source-scan test,
mirroring replay/executor.py's own "no LLM anywhere in this module" check).
It only reads already-saved artifact JSON (via artifact/store.py) and does
plain string matching against a goal sentence — the entire "how do I do this
in the UI" question stays with agent/loop.py's discovery loop, and the "how
do I execute this deterministically" question stays with
replay/executor.py. This module answers a narrower, system-level question:
does an existing reusable capability already cover this goal, and if so,
what are its typed inputs, going by what's actually in this goal string?

Two decisions only, matching the assignment's contract exactly:

  - "replay": an existing capability's description matches the goal closely
    enough, AND every one of its required typed inputs could be confidently
    extracted from the goal text. Never a guess — see _extract_inputs.
  - "discover": either no capability matched closely enough, or one matched
    but its required inputs couldn't all be confidently extracted. Fails
    closed rather than confidently picking (or party-filling) the wrong
    capability; the original goal/target flow straight into discovery
    unchanged, so the task still gets done, just via a fresh discovery run.

Capability matching and input extraction are both plain, deterministic text
heuristics — no embeddings, no vector search, no ranking model, no LLM call.
This is a real, stated trade-off: the extraction heuristic (see
_extract_inputs) only understands "X from A to B"-shaped goals, using each
input's own to_/from_ name prefix (if any) to decide which clause to look
in — nothing here is specific to any one capability's field names. A goal
phrased very differently may fail to extract even when a human would
recognize the capability; that failure is safe (falls back to discovery),
never silently wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

from artifact.schema import Artifact, InputParam, ParamType
from artifact.store import latest_version_path, list_capability_ids, load_path

# Fraction of a candidate capability's own descriptive vocabulary that must
# appear in the goal text for it to be considered a match at all. Tunable,
# but deliberately conservative — fail closed to discovery over a weak match.
_MATCH_THRESHOLD = 0.3

# Generic function words plus common structural nouns that introduce an
# identifier/attribute rather than being one ("member 10001", "X account") --
# not specific to any one capability's domain vocabulary.
_GENERIC_STOPWORDS = {
    "from", "to", "the", "a", "an", "of", "and", "then", "please", "exactly", "reach",
    "member", "account", "id",
}


@dataclass
class RouteDecision:
    action: Literal["replay", "discover"]
    capability_id: Optional[str] = None
    artifact_path: Optional[Path] = None
    params: dict[str, str] = field(default_factory=dict)
    reason: str = ""


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _candidate_vocabulary(capability_id: str, artifact: Artifact) -> set[str]:
    return _tokenize(artifact.description) | _tokenize(artifact.goal_template) | _tokenize(capability_id.replace("-", " "))


def _match_capability(goal: str, candidates: list[tuple[str, Artifact]]) -> Optional[str]:
    """Deterministic, dependency-free capability matching: score each known
    capability by what fraction of *its own* descriptive vocabulary shows up
    in the goal text, and pick the best-scoring one only if it clears
    _MATCH_THRESHOLD. No semantic search, no LLM, no external model.
    """
    goal_tokens = _tokenize(goal)
    best_id: Optional[str] = None
    best_score = 0.0
    for capability_id, artifact in candidates:
        vocab = _candidate_vocabulary(capability_id, artifact)
        if not vocab:
            continue
        score = len(goal_tokens & vocab) / len(vocab)
        if score > best_score:
            best_score, best_id = score, capability_id
    if best_id is not None and best_score >= _MATCH_THRESHOLD:
        return best_id
    return None


_NUMBER_RE = re.compile(r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z]+")
_FROM_RE = re.compile(r"\bfrom\b", re.IGNORECASE)
_TO_RE = re.compile(r"\bto\b", re.IGNORECASE)
# A word immediately qualifying an "account" noun phrase ("savings
# sub-account", "checking account") is an unambiguous, generic English
# pattern for "what kind of account" — a much stronger signal than "last
# content word in the clause", which breaks down as soon as a clause
# describes more than one thing (an account type AND a deposit amount, say).
# Tried first for STRING inputs; "last content word" remains the fallback
# for goals that never say "account" at all (e.g. this project's own
# transfer-funds example).
_ACCOUNT_QUALIFIER_RE = re.compile(r"\b([A-Za-z]+)\s+(?:sub[\s-]*)?account\b", re.IGNORECASE)


def _segment_by_from_to(goal: str) -> dict[str, str]:
    """Splits the goal into labeled clauses ("before" "from", the "from ...
    to" clause, "after" "to") using "from"/"to" as generic English
    prepositions — not specific to any one capability's vocabulary. A clause
    is empty if its keyword isn't present.
    """
    from_m = _FROM_RE.search(goal)
    to_m = _TO_RE.search(goal, from_m.end() if from_m else 0)
    before_from = goal[: from_m.start()] if from_m else goal
    if from_m and to_m:
        from_seg, after_to = goal[from_m.end() : to_m.start()], goal[to_m.end() :]
    elif from_m:
        from_seg, after_to = goal[from_m.end() :], ""
    elif to_m:
        from_seg, after_to = "", goal[to_m.end() :]
    else:
        from_seg = after_to = ""
    return {"before": before_from, "from": from_seg, "to": after_to}


def _segments_for(name: str) -> list[str]:
    """Which labeled clause(s) to search for this input's value, in
    preference order — decided purely from the input's OWN declared name
    prefix, never from what capability it belongs to.
    """
    if name.startswith("to_") or name == "to":
        return ["to", "from", "before"]
    if name.startswith("from_") or name == "from":
        return ["from", "before", "to"]
    # A bare param (no to_/from_ prefix) most often names the primary/source
    # entity in "X from A to B" phrasing (e.g. member_id vs to_member_id), so
    # it belongs in the "from" clause more often than the lead clause — fall
    # back to the lead clause only if nothing unclaimed is there.
    return ["from", "to", "before"]


def _find_in_segment(
    param: InputParam, label: str, seg: str, claimed: set[tuple[str, int, int]]
) -> Optional[tuple[str, tuple[str, int, int]]]:
    """Looks for one param's value within one labeled clause. For a STRING
    input, an "account"-qualifying word ("savings sub-account", "checking
    account") is tried first — precise and unambiguous wherever it applies.
    Falling back to "the last unclaimed content word in the clause" only
    when that pattern isn't present keeps that weaker heuristic scoped to
    goals that never say "account" at all (this project's own transfer
    example), rather than applying it everywhere a clause happens to
    describe more than one thing.
    """
    is_number = param.type == ParamType.NUMBER
    if not is_number:
        m = _ACCOUNT_QUALIFIER_RE.search(seg)
        if m:
            key = (label, m.start(1), m.end(1))
            if key not in claimed:
                return m.group(1), key

    pattern = _NUMBER_RE if is_number else _WORD_RE
    found = None
    for m in pattern.finditer(seg):
        token = m.group(1) if is_number else m.group(0)
        if not is_number and token.lower() in _GENERIC_STOPWORDS:
            continue
        key = (label, m.start(), m.end())
        if key in claimed:
            continue
        candidate = (token.replace(",", "") if is_number else token, key)
        if is_number:
            return candidate  # a quantity/id is usually the FIRST number in its clause
        found = candidate  # otherwise keep the LAST unclaimed content word in the clause
                            # ("Member 10001 checking" -- the qualifier trails the noun)
    return found


def _extract_inputs(goal: str, inputs: list[InputParam]) -> dict[str, str]:
    """Best-effort, deterministic extraction of each declared input's value
    from the goal text. Never guesses across inputs: a value is claimed at
    most once, keyed by (clause label, local span) so two DIFFERENT clauses
    that happen to have the same local offset (e.g. both "... checking")
    never falsely collide. Any input this can't confidently fill is simply
    absent from the result — the caller (route()) treats an incomplete
    result as "fail closed to discovery", never as a partial/best-guess
    replay.
    """
    segments = _segment_by_from_to(goal)
    claimed: set[tuple[str, int, int]] = set()
    extracted: dict[str, str] = {}

    # to_/from_-prefixed inputs have an unambiguous clause to search — bind
    # them first so a bare/primary-subject input doesn't accidentally claim
    # a value that actually belongs to one of them.
    ordered = sorted(inputs, key=lambda p: 0 if (p.name.startswith("to_") or p.name.startswith("from_")) else 1)

    for param in ordered:
        for label in _segments_for(param.name):
            seg = segments[label]
            if not seg:
                continue
            found = _find_in_segment(param, label, seg, claimed)
            if found is not None:
                value, key = found
                extracted[param.name] = value
                claimed.add(key)
                break

    return extracted


def route(goal: str, target_url: str) -> RouteDecision:
    """The single system-level decision this module makes: replay an
    existing capability, or fall back to discovery. Never decides *how* to
    do either — replay_artifact()/run_discovery() own that.
    """
    candidates: list[tuple[str, Artifact]] = []
    for capability_id in list_capability_ids():
        path = latest_version_path(capability_id)
        if path is None:
            continue
        candidates.append((capability_id, load_path(path)))

    capability_id = _match_capability(goal, candidates)
    if capability_id is None:
        return RouteDecision(action="discover", reason="no existing capability's description matched this goal closely enough")

    artifact = next(a for cid, a in candidates if cid == capability_id)
    extracted = _extract_inputs(goal, artifact.inputs)

    missing = [p.name for p in artifact.inputs if p.required and p.name not in extracted]
    if missing:
        return RouteDecision(
            action="discover",
            reason=f"matched capability {capability_id!r} but could not confidently extract required input(s) {missing} from the goal text",
        )

    return RouteDecision(
        action="replay",
        capability_id=capability_id,
        artifact_path=latest_version_path(capability_id),
        params=extracted,
        reason=f"matched existing capability {capability_id!r}; all required inputs extracted from the goal text",
    )
