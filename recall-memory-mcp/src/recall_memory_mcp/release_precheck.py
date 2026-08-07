"""Task 10.9 — pre-release re-verification (decision logic only, no network).

Before a release we must re-confirm two things that were true when the gates
were first passed but are owned by third parties and can change under us:

1. the distribution name is still unregistered, and the neighbouring
   ``recall-mcp`` project still exists (so we never inherit or impersonate it);
2. the official client documents and the ``mcp`` SDK contract we built against
   have not moved.  If they have, Gate 3 (SDK/protocol) and/or Gate 7 (host
   assets) must be reopened.

Everything here is deliberately network-free.  The live driver fetches and
hands in :class:`SourceProbe` records.  That split exists so the *fail-closed*
rules can actually be unit-tested — which matters more than the fetching does,
because the realistic accident is not "the check reported FAIL", it is a check
reporting PASS on evidence that was never collected:

* a vendor host that timed out and was quietly skipped,
* a source dropped from the source list so it can never disagree,
* a report generated for one commit and reused for a later release.

:func:`overall_verdict` therefore compares against an *expected* source set
rather than against whatever happened to be probed, treats any unreachable or
non-200 source as ``INCOMPLETE``, and :meth:`PrecheckReport.is_valid_for` binds
the report to a specific commit and a wall-clock expiry.

Only public vendor/registry endpoints are ever involved.  No tokens, no
credentials, no private paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "DRIFT_DIGEST",
    "DRIFT_HTTP_ERROR",
    "DRIFT_NEW",
    "DRIFT_UNCHANGED",
    "DRIFT_UNREACHABLE",
    "VERDICT_FAIL",
    "VERDICT_INCOMPLETE",
    "VERDICT_PASS",
    "NameExpectation",
    "NameResult",
    "PrecheckReport",
    "SourceBaseline",
    "SourceProbe",
    "classify_source",
    "evaluate_name",
    "is_tickable",
    "load_baselines",
    "overall_verdict",
    "reopened_gates",
    "sdk_within_pin",
]

# --- per-source drift verdicts -------------------------------------------

DRIFT_UNCHANGED = "UNCHANGED"
DRIFT_DIGEST = "DIGEST_DRIFT"
DRIFT_UNREACHABLE = "UNREACHABLE"
DRIFT_HTTP_ERROR = "HTTP_ERROR"
DRIFT_NEW = "NEW"

#: verdicts that mean "we did not actually observe this source"
_NOT_OBSERVED = frozenset({DRIFT_UNREACHABLE, DRIFT_HTTP_ERROR})

# --- overall verdicts -----------------------------------------------------

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True)
class SourceBaseline:
    """A digest recorded by an earlier provenance run (phase7-doc-sources.json)."""

    source_id: str
    url: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class SourceProbe:
    """One live fetch attempt performed by the driver."""

    source_id: str
    url: str
    http_status: int | None = None
    bytes: int | None = None
    sha256: str | None = None
    error: str | None = None

    @property
    def reachable(self) -> bool:
        return self.error is None and self.http_status is not None


def classify_source(baseline: SourceBaseline | None, probe: SourceProbe) -> str:
    """Classify one source.

    The ordering matters.  Transport and HTTP problems are decided *before* any
    digest comparison, because a 404 page has a perfectly stable digest and
    would otherwise be silently compared as though it were the document.
    """

    if probe.error is not None or probe.http_status is None:
        return DRIFT_UNREACHABLE
    if probe.http_status != 200:
        return DRIFT_HTTP_ERROR
    if not probe.sha256:
        # A driver that forgot to hash must not read as "same as before".
        return DRIFT_UNREACHABLE
    if baseline is None:
        return DRIFT_NEW
    return DRIFT_UNCHANGED if probe.sha256 == baseline.sha256 else DRIFT_DIGEST


def load_baselines(raw: Mapping[str, Any]) -> dict[str, SourceBaseline]:
    """Load baselines from a ``phase7-doc-sources.json`` style payload.

    Entries without a digest (the previous run could not fetch them either) are
    dropped rather than materialised with an empty hash, so a later comparison
    cannot accidentally succeed against nothing.
    """

    baselines: dict[str, SourceBaseline] = {}
    for entry in raw.get("sources", []) or []:
        digest = entry.get("sha256")
        source_id = entry.get("id")
        if not digest or not source_id:
            continue
        baselines[source_id] = SourceBaseline(
            source_id=source_id,
            url=entry.get("url", ""),
            sha256=digest,
            bytes=int(entry.get("bytes") or 0),
        )
    return baselines


# --- name availability ----------------------------------------------------


@dataclass(frozen=True)
class NameExpectation:
    """What a registry endpoint must answer for the naming story to still hold."""

    source_id: str
    url: str
    accepted_statuses: tuple[int, ...]
    meaning: str


@dataclass(frozen=True)
class NameResult:
    source_id: str
    ok: bool
    observed_status: int | None
    detail: str
    #: did the registry actually answer?  "the name is taken" and "I could not
    #: reach the registry" are different facts and must not collapse into one
    #: verdict: the first is a release blocker, the second is missing evidence.
    observed: bool = True


def evaluate_name(expectation: NameExpectation, probe: SourceProbe) -> NameResult:
    if not probe.reachable:
        return NameResult(
            expectation.source_id,
            False,
            None,
            f"unreachable ({probe.error or 'no status'}); "
            f"cannot confirm '{expectation.meaning}'",
            observed=False,
        )
    ok = probe.http_status in expectation.accepted_statuses
    expected = "/".join(str(code) for code in expectation.accepted_statuses)
    if ok:
        detail = f"HTTP {probe.http_status}: still {expectation.meaning}"
    else:
        detail = (
            f"HTTP {probe.http_status} but expected {expected}: "
            f"no longer {expectation.meaning}"
        )
    return NameResult(expectation.source_id, ok, probe.http_status, detail, observed=True)


# --- SDK pin --------------------------------------------------------------

_VERSION_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s*(?:([abc]|rc|alpha|beta|dev|post)\S*)?\s*$")
_SPEC_RE = re.compile(r"(>=|<=|==|!=|~=|<|>)\s*([\dA-Za-z.\-+*]+)")


def _parse_release(version: str) -> tuple[tuple[int, ...], bool] | None:
    match = _VERSION_RE.match(version)
    if not match:
        return None
    release = tuple(int(part) for part in match.group(1).split("."))
    return release, match.group(2) is not None


def _pad(release: tuple[int, ...], length: int) -> tuple[int, ...]:
    return release + (0,) * (length - len(release))


def sdk_within_pin(version: str | None, spec: str) -> bool:
    """Is ``version`` accepted by a pin such as ``mcp>=2.0,<2.1``?

    Pre-releases are rejected on purpose: pip does not resolve to a pre-release
    unless explicitly asked, so ``2.1.0rc1`` appearing upstream is *not* the
    version our users would install and must not be read as "still in range"
    either.  An unknown or unparseable version is likewise never in range —
    the point of this check is to notice movement, and "I could not tell" is
    not "nothing moved".
    """

    if not version:
        return False
    parsed = _parse_release(version)
    if parsed is None:
        return False
    release, is_prerelease = parsed
    if is_prerelease:
        return False

    constraints = _SPEC_RE.findall(spec)
    if not constraints:
        return False
    for operator, bound_text in constraints:
        bound_parsed = _parse_release(bound_text)
        if bound_parsed is None:
            return False
        bound = bound_parsed[0]
        width = max(len(release), len(bound))
        left, right = _pad(release, width), _pad(bound, width)
        if operator == ">=" and not left >= right:
            return False
        if operator == ">" and not left > right:
            return False
        if operator == "<=" and not left <= right:
            return False
        if operator == "<" and not left < right:
            return False
        if operator == "==" and left != right:
            return False
        if operator == "!=" and left == right:
            return False
    return True


# --- overall verdict ------------------------------------------------------


def reopened_gates(*, sdk_in_pin: bool, ungrounded_claims: Sequence[str]) -> tuple[str, ...]:
    """Which gates a failure re-opens, named after what actually broke."""

    gates: list[str] = []
    if not sdk_in_pin:
        gates.append("Gate 3")
    if ungrounded_claims:
        gates.append("Gate 7")
    return tuple(gates)


def overall_verdict(
    *,
    source_verdicts: Mapping[str, str],
    name_results: Sequence[NameResult],
    ungrounded_claims: Sequence[str],
    claims_checked: int,
    sdk_in_pin: bool,
    expected_sources: Iterable[str],
) -> str:
    """PASS only when the run was both complete and clean.

    ``FAIL`` dominates ``INCOMPLETE``: if a real contract break is already
    visible, an unrelated timeout must not downgrade it to "we could not
    finish".
    """

    if (
        ungrounded_claims
        or not sdk_in_pin
        or any(r.observed and not r.ok for r in name_results)
    ):
        return VERDICT_FAIL

    expected = set(expected_sources)
    if not expected.issubset(source_verdicts.keys()):
        return VERDICT_INCOMPLETE
    if any(source_verdicts[name] in _NOT_OBSERVED for name in expected):
        return VERDICT_INCOMPLETE
    if any(not r.observed for r in name_results):
        return VERDICT_INCOMPLETE
    if claims_checked <= 0:
        return VERDICT_INCOMPLETE
    return VERDICT_PASS


def is_tickable(verdict: str) -> bool:
    """Only a complete, clean run may tick task 10.9."""

    return verdict == VERDICT_PASS


# --- report ---------------------------------------------------------------


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class PrecheckReport:
    """A 10.9 result that knows what it is *not* valid for.

    A pre-release check is only meaningful for the release it immediately
    precedes, so the report carries the commit it describes and an explicit
    expiry.  Both are enforced by :meth:`is_valid_for` rather than left as
    prose in a text file.
    """

    generated_at: str
    head_commit: str
    verdict: str
    validity_hours: int = 24
    sources: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    name_results: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    claims_checked: int = 0
    ungrounded_claims: Sequence[str] = field(default_factory=tuple)
    sdk_latest: str | None = None
    sdk_pin: str = ""
    sdk_in_pin: bool = False
    reopened_gates: tuple[str, ...] = ()

    def expires_at(self) -> str:
        return (
            _parse_iso(self.generated_at) + timedelta(hours=self.validity_hours)
        ).isoformat(timespec="seconds")

    def is_valid_for(self, release_commit: str, *, now: str) -> bool:
        if self.verdict != VERDICT_PASS:
            return False
        if release_commit != self.head_commit:
            return False
        return _parse_iso(now) < _parse_iso(self.expires_at())

    def to_json(self) -> dict[str, Any]:
        return {
            "task": "10.9",
            "generated_at": self.generated_at,
            "expires_at": self.expires_at(),
            "validity_hours": self.validity_hours,
            "head_commit": self.head_commit,
            "verdict": self.verdict,
            "tickable": is_tickable(self.verdict),
            "claims_checked": self.claims_checked,
            "ungrounded_claims": list(self.ungrounded_claims),
            "sdk_pin": self.sdk_pin,
            "sdk_latest": self.sdk_latest,
            "sdk_in_pin": self.sdk_in_pin,
            "reopened_gates": list(self.reopened_gates),
            "sources": [dict(row) for row in self.sources],
            "name_results": [dict(row) for row in self.name_results],
        }

    def render(self) -> str:
        lines: list[str] = []
        emit = lines.append
        emit("Task 10.9 — pre-release re-verification")
        emit("=" * 60)
        emit(f"generated_at : {self.generated_at}")
        emit(f"expires_at   : {self.expires_at()}  (validity {self.validity_hours}h)")
        emit(f"head_commit  : {self.head_commit}")
        emit("")
        emit("This report is valid ONLY for a release cut from the commit above,")
        emit("and only before the expiry above.  Any later commit invalidates it;")
        emit("re-run the check immediately before publishing.")
        emit("")

        emit("== registry / name checks ==")
        for row in self.name_results:
            if not row.get("observed", True):
                mark = "UNSEEN"
            elif row.get("ok"):
                mark = "OK    "
            else:
                mark = "FAIL  "
            emit(f"  [{mark}] {row.get('id', '?'):<24} {row.get('detail', '')}")
        if not self.name_results:
            emit("  (none)")
        emit("")

        emit("== vendor document drift ==")
        for row in self.sources:
            emit(
                f"  {str(row.get('verdict', '?')):<13} {str(row.get('id', '?')):<28} "
                f"HTTP {row.get('http_status')}  {row.get('url', '')}"
            )
        if not self.sources:
            emit("  (none)")
        emit("")

        emit("== SDK contract ==")
        emit(f"  pin      : {self.sdk_pin}")
        emit(f"  latest   : {self.sdk_latest}")
        emit(f"  in range : {self.sdk_in_pin}")
        emit("")

        emit("== claim grounding ==")
        emit(f"  claims checked : {self.claims_checked}")
        emit(f"  ungrounded     : {len(self.ungrounded_claims)}")
        for claim in self.ungrounded_claims:
            emit(f"    - {claim}")
        emit("")

        if self.reopened_gates:
            emit(f"REOPENED: {', '.join(self.reopened_gates)}")
            emit("")

        emit(f"VERDICT: {self.verdict}")
        if is_tickable(self.verdict):
            emit("Task 10.9 may be ticked for this commit, until the expiry above.")
        else:
            emit("Task 10.9 is NOT tickable from this run.")
        return "\n".join(lines) + "\n"
