"""The Task AI entry point's routing decision: goal + target, replay an
existing capability, or fall back to discovery.

This module is deliberately not a computer-use agent. It never touches
Playwright, never reasons about individual UI elements, and never calls an
LLM (verified by tests/test_router.py's source-scan test). It only reads
saved artifact JSON and does plain string matching against a goal sentence.

Two decisions only, matching the assignment's contract:

  - "replay": an existing capability's description matches the goal closely
    enough, and every one of its required typed inputs could be confidently
    extracted from the goal text. Never a guess, see _extract_inputs.
  - "discover": either no capability matched, or one matched but its
    required inputs could not all be confidently extracted. Fails closed
    rather than picking or partially filling the wrong capability.

Capability matching and input extraction are both plain, deterministic text
heuristics, no embeddings, no vector search, no LLM call. This is a real,
stated trade-off: the extraction heuristic (see _extract_inputs) only
understands "X from A to B"-shaped goals, using each input's own to_/from_
name prefix to decide which clause to look in. A goal phrased very
differently may fail to extract even when a human would recognize the
capability; that failure is safe (falls back to discovery), never silently
wrong.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlparse

from artifact.schema import Artifact, InputParam, ParamType
from artifact.store import latest_version_path, list_capability_ids, load_path

logger = logging.getLogger(__name__)

# Weighted-overlap fraction of a candidate's own vocabulary that must appear
# in the goal text to be considered a match at all. Deliberately
# conservative: fail closed to discovery over a weak match.
_MATCH_THRESHOLD = 0.3

# Generic function words plus common structural nouns that introduce an
# identifier/attribute rather than being one, not specific to any domain.
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


def _target_compatible(target_url: str, artifact: Artifact) -> bool:
    """Whether this capability was recorded against the same deployment the
    caller is targeting. TargetApp.app_id is documented as stable across
    tenants precisely so a capability can be reused across base_urls of the
    same app, but the router's only signal here is the target_url string
    itself, so the practical proxy is: same host the capability was recorded
    against. Not a full-URL match; entry_path is where steps navigate from,
    not part of what "target" means at this entry point."""
    requested = urlparse(target_url)
    recorded = urlparse(artifact.target_app.base_url)
    if requested.hostname and recorded.hostname:
        return (requested.hostname, requested.port) == (recorded.hostname, recorded.port)
    return target_url.rstrip("/") == artifact.target_app.base_url.rstrip("/")


def _document_frequencies(vocabs: list[set[str]]) -> dict[str, int]:
    df: dict[str, int] = {}
    for vocab in vocabs:
        for token in vocab:
            df[token] = df.get(token, 0) + 1
    return df


def _weighted_overlap_score(goal_tokens: set[str], vocab: set[str], df: dict[str, int], n_candidates: int) -> float:
    """Token-overlap score weighted by inverse document frequency across the
    current candidate pool, recomputed fresh each call. Fixes a real
    false-positive: a flat overlap score let boilerplate every capability's
    goal_template shares (generic connectives, this project's own trailing
    "reach the confirmation screen") score as high as a capability's actual
    distinguishing vocabulary. Down-weighting by rarity makes shared
    boilerplate contribute close to nothing."""
    if not vocab:
        return 0.0

    def idf(token: str) -> float:
        return math.log((n_candidates + 1) / (df.get(token, 0) + 1)) + 1.0

    matched_weight = sum(idf(t) for t in (goal_tokens & vocab))
    total_weight = sum(idf(t) for t in vocab)
    return matched_weight / total_weight if total_weight else 0.0


def _distinctive_tokens(vocab: set[str], df: dict[str, int]) -> set[str]:
    """Tokens in this capability's vocabulary that no other candidate in the
    pool shares, computed fresh each call. With only one candidate this
    degrades to the whole vocabulary, the correct answer when there is
    nothing to compare against."""
    return {t for t in vocab if df.get(t, 0) <= 1}


def _workflow_compatible(goal_tokens: set[str], vocab: set[str], df: dict[str, int]) -> bool:
    """Gate applied to the best-scoring candidate: the goal must share at
    least one token actually distinctive to this capability, not just
    generic wording every candidate's description uses. This is what stops
    "update the phone number for member 10001" from matching transfer-funds
    just because both goals mention "member"."""
    return bool(goal_tokens & _distinctive_tokens(vocab, df))


def _match_capability(
    goal_tokens: set[str], candidates: list[tuple[str, Artifact]]
) -> tuple[Optional[str], dict[str, set[str]], dict[str, int]]:
    """Scores each target-filtered candidate by weighted vocabulary overlap
    and picks the best one, only if it clears _MATCH_THRESHOLD. Also returns
    the per-candidate vocabularies and document frequencies so the caller
    can run the workflow-compatibility gate without recomputing them."""
    vocabs = {cid: _candidate_vocabulary(cid, artifact) for cid, artifact in candidates}
    df = _document_frequencies(list(vocabs.values()))

    best_id: Optional[str] = None
    best_score = 0.0
    for capability_id in vocabs:
        score = _weighted_overlap_score(goal_tokens, vocabs[capability_id], df, len(candidates))
        if score > best_score:
            best_score, best_id = score, capability_id

    if best_id is not None and best_score >= _MATCH_THRESHOLD:
        return best_id, vocabs, df
    return None, vocabs, df


_NUMBER_RE = re.compile(r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z]+")
_FROM_RE = re.compile(r"\bfrom\b", re.IGNORECASE)
_TO_RE = re.compile(r"\bto\b", re.IGNORECASE)
# A word immediately qualifying an "account" noun phrase ("savings
# sub-account") is an unambiguous generic pattern for "what kind of
# account", stronger than "last content word in the clause". Tried first for
# STRING inputs; "last content word" is the fallback for goals that never
# say "account" at all.
_ACCOUNT_QUALIFIER_RE = re.compile(r"\b([A-Za-z]+)\s+(?:sub[\s-]*)?account\b", re.IGNORECASE)


def _segment_by_from_to(goal: str) -> dict[str, str]:
    """Splits the goal into labeled clauses using "from"/"to" as generic
    prepositions. A clause is empty if its keyword is not present."""
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
    preference order, decided from the input's own name prefix."""
    if name.startswith("to_") or name == "to":
        return ["to", "from", "before"]
    if name.startswith("from_") or name == "from":
        return ["from", "before", "to"]
    # A bare param most often names the primary/source entity in "X from A to
    # B" phrasing, so it belongs in the "from" clause more often than the
    # lead clause.
    return ["from", "to", "before"]


def _find_in_segment(
    param: InputParam, label: str, seg: str, claimed: set[tuple[str, int, int]]
) -> Optional[tuple[str, tuple[str, int, int]]]:
    """Looks for one param's value within one labeled clause. For a STRING
    input, an account-qualifying word is tried first since it is precise
    wherever it applies. Falls back to the last unclaimed content word in
    the clause only when that pattern is absent."""
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
            return candidate  # a quantity/id is usually the first number in its clause
        found = candidate  # otherwise keep the last unclaimed content word in the clause
    return found


def _extract_inputs(goal: str, inputs: list[InputParam]) -> dict[str, str]:
    """Best-effort, deterministic extraction of each declared input's value
    from the goal text. A value is claimed at most once, keyed by (clause
    label, local span) so two different clauses at the same local offset
    never falsely collide. Any input this cannot confidently fill is simply
    absent from the result; route() treats an incomplete result as fail
    closed to discovery, never a partial replay."""
    segments = _segment_by_from_to(goal)
    claimed: set[tuple[str, int, int]] = set()
    extracted: dict[str, str] = {}

    # to_/from_-prefixed inputs have an unambiguous clause to search; bind them
    # first so a bare/primary-subject input does not claim a value that
    # actually belongs to one of them.
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
    existing capability, or fall back to discovery. Never decides how to do
    either, replay_artifact()/run_discovery() own that.

    Each stage is a hard gate; any failure falls straight to discover, never
    a partial match carried forward: target compatibility, then
    workflow-vocabulary match and compatibility, then required typed inputs
    extractable.
    """
    all_candidates: list[tuple[str, Artifact]] = []
    for capability_id in list_capability_ids():
        path = latest_version_path(capability_id)
        if path is None:
            continue
        all_candidates.append((capability_id, load_path(path)))

    candidates = [(cid, a) for cid, a in all_candidates if _target_compatible(target_url, a)]
    if not candidates:
        logger.info("route decision=discover reason=no_target_match goal=%r", goal)
        return RouteDecision(action="discover", reason="no existing capability was recorded against this target")

    goal_tokens = _tokenize(goal)
    capability_id, vocabs, df = _match_capability(goal_tokens, candidates)
    if capability_id is None:
        logger.info("route decision=discover reason=no_vocabulary_match goal=%r", goal)
        return RouteDecision(action="discover", reason="no existing capability's description matched this goal closely enough")

    if not _workflow_compatible(goal_tokens, vocabs[capability_id], df):
        logger.info("route decision=discover reason=not_workflow_compatible candidate=%s goal=%r", capability_id, goal)
        return RouteDecision(
            action="discover",
            reason=(
                f"goal shares only generic/common wording with capability {capability_id!r}, "
                "not its own distinguishing vocabulary, treating this as a different workflow"
            ),
        )

    artifact = next(a for cid, a in candidates if cid == capability_id)
    extracted = _extract_inputs(goal, artifact.inputs)

    missing = [p.name for p in artifact.inputs if p.required and p.name not in extracted]
    if missing:
        logger.info("route decision=discover reason=missing_inputs candidate=%s missing=%s", capability_id, missing)
        return RouteDecision(
            action="discover",
            reason=f"matched capability {capability_id!r} but could not confidently extract required input(s) {missing} from the goal text",
        )

    logger.info("route decision=replay capability=%s params=%s", capability_id, sorted(extracted))
    return RouteDecision(
        action="replay",
        capability_id=capability_id,
        artifact_path=latest_version_path(capability_id),
        params=extracted,
        reason=f"matched existing capability {capability_id!r}; all required inputs extracted from the goal text",
    )
