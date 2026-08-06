"""Phase 8 task 8.1 — the cross-client canary scope and content.

Phase 8 proves the product claim: a memory written by one real host is read
back by another real host against the same authority.  That proof is only
worth anything if the record under test is

* **unique per run** — otherwise a re-run reads yesterday's row and passes for
  the wrong reason;
* **synthetic** — the canary is copied into artifacts, review notes and issue
  trackers, so it must never carry personal data or a credential;
* **legal input** — it has to validate against the same strict request models
  the tools enforce, or the run fails on the canary rather than on the host;
* **serialisable** — steps 8.2 - 8.10 happen by hand across three different
  applications, so the plan lives in a JSON artifact rather than in someone's
  head.

This module is transport-free and I/O-free: it builds and validates the plan.
It does **not** perform the cross-client run, and importing it proves nothing
about ChatGPT Desktop, Claude Desktop or Kiro.

Revision ladder pinned here (tasks 8.2 / 8.4 / 8.6):

===========  ==========================  ==========
step         operation                   revision
===========  ==========================  ==========
8.2          ``memory_add``              1
8.4          ``memory_replace`` (exp 1)  2
8.6          ``memory_remove`` (exp 2)   3 (tombstone)
===========  ==========================  ==========
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import models

CANARY_SCHEMA_VERSION: Final[str] = "1.0.0"

#: A dedicated project scope. Never ``global``: a canary must not be dropped
#: into the scope that holds the user's real shared memory.
CANARY_SCOPE_PREFIX: Final[str] = "project:recall-canary-"

CANARY_KIND: Final[str] = "canary"
CANARY_TAGS: Final[tuple[str, ...]] = ("recall-canary", "phase8", "synthetic")

#: 12 hex chars = 48 bits. Collision risk across the handful of canary runs a
#: human will ever perform is negligible, and the scope stays short enough to
#: be pasted into a host UI.
RUN_ID_BYTES: Final[int] = 6
RUN_ID_PATTERN: Final[str] = r"^[0-9a-f]{12,32}$"

_RUN_ID_RE = re.compile(RUN_ID_PATTERN)

_ADD_TEMPLATE: Final[str] = (
    "Recall cross-client canary {run_id} revision 1. "
    "Synthetic phase 8 record with no personal data and no credentials. "
    "Created by step 8.2 so another host can read it back."
)
_REPLACE_TEMPLATE: Final[str] = (
    "Recall cross-client canary {run_id} revision 2. "
    "Replaced by step 8.4 from a different host to prove optimistic locking. "
    "Still synthetic, still free of personal data."
)


# --------------------------------------------------------------------------
# sensitive-content scanner
# --------------------------------------------------------------------------
#
# `redaction.redact_text` answers "would this leak if logged". That is not the
# same question as "does this contain personal data". A phone number, an email
# address or a national ID survives `redact_text` untouched, so task 8.1 needs
# its own detector set. Both checks are applied to the canary.

_LUHN_CANDIDATE = re.compile(r"(?<![0-9])(?:[0-9][ -]?){13,19}(?![0-9])")

#: Lowercase-hex tokens of >= 12 chars that contain at least one ``a-f`` are
#: digests/run ids, not identity numbers. They are masked out before the
#: "long digit run" detector so a sha256 that happens to start with nine
#: digits does not masquerade as a national ID. A pure-digit token is never
#: masked, which is what keeps the detector honest.
_HEX_DIGEST = re.compile(
    r"(?<![0-9A-Za-z])(?=[0-9a-f]{12,}(?![0-9A-Za-z]))(?=[0-9a-f]*[a-f])[0-9a-f]+"
)

_DETECTORS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("phone", re.compile(r"\+[0-9][0-9 ()-]{6,18}[0-9]")),
    ("ipv4", re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])")),
    ("unc_path", re.compile(r"\\\\[^\s\"'<>|]+")),
    ("windows_path", re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/][^\s\"'<>|)\]]*")),
    (
        "posix_system_path",
        re.compile(
            r"(?<![\w.])/(?:home|Users|users|root|private|mnt|var|tmp|etc|opt|usr|proc)"
            r"(?:/[^\s\"'<>|,)\]]*)*"
        ),
    ),
    ("url_with_credentials", re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}")),
    ("bearer", re.compile(r"(?i)\b(?:Bearer|Basic|Digest|DPoP)\s+[A-Za-z0-9._~+/=-]{6,}")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{12,}")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")),
    (
        "kv_secret",
        re.compile(
            r"(?i)\b(?:[A-Za-z0-9]+[_-])*"
            r"(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|id[_-]?token"
            r"|client[_-]?secret|secret|token|password|passwd|pwd|private[_-]?key"
            r"|session[_-]?key|key|cookie)\b\s*[\"']?\s*[:=]\s*[\"']?[^\s\"',}&]+"
        ),
    ),
    ("non_ascii", re.compile(r"[^\x20-\x7e]")),
)

#: Applied after `_HEX_DIGEST` masking; catches SSN / national-ID / phone-like
#: runs that the shaped detectors above would miss.
_LONG_DIGIT_RUN = re.compile(r"(?<![0-9A-Za-z_-])[0-9]{9,}(?![0-9A-Za-z_-])")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def scan_sensitive(text: str, *, extra_terms: Iterable[str] = ()) -> tuple[str, ...]:
    """Return the names of every sensitive shape found in ``text``.

    An empty tuple means "clean". ``extra_terms`` is a case-insensitive
    substring blocklist for values only the caller knows about — typically the
    local OS username, so an artifact can be proven free of it.
    """

    findings: list[str] = []
    for name, pattern in _DETECTORS:
        if pattern.search(text):
            findings.append(name)

    masked = _HEX_DIGEST.sub("<digest>", text)
    if _LONG_DIGIT_RUN.search(masked):
        findings.append("long_digit_run")

    for match in _LUHN_CANDIDATE.finditer(masked):
        digits = re.sub(r"[^0-9]", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            findings.append("credit_card")
            break

    lowered = text.lower()
    for term in extra_terms:
        term = term.strip().lower()
        if term and term in lowered:
            findings.append("extra_term")
            break

    return tuple(dict.fromkeys(findings))


# --------------------------------------------------------------------------
# plan model
# --------------------------------------------------------------------------


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CanaryWrite(_Frozen):
    """One write step of the ladder."""

    operation: Literal["add", "replace", "remove"]
    task: str
    idempotency_key: models.IdempotencyKey
    expected_revision_after: Annotated[int, Field(ge=1)]
    content: str | None = None
    content_sha256: str | None = None
    expected_revision: Annotated[int, Field(ge=1)] | None = None


class CanaryPlan(_Frozen):
    """The single canary scope + content required by task 8.1."""

    schema_version: Literal["1.0.0"] = CANARY_SCHEMA_VERSION
    run_id: Annotated[str, Field(pattern=RUN_ID_PATTERN)]
    created_at: str
    scope: models.Scope
    kind: models.Kind = CANARY_KIND
    tags: models.Tags = CANARY_TAGS
    add: CanaryWrite
    replace: CanaryWrite
    remove: CanaryWrite

    # -- serialisation ---------------------------------------------------
    def to_json(self) -> str:
        return self.model_dump_json(indent=2, exclude_none=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> CanaryPlan:
        return cls.model_validate_json(text)

    # -- safety ----------------------------------------------------------
    def scannable_values(self) -> tuple[str, ...]:
        """Every string *value* in the serialised plan.

        Keys are excluded on purpose: ``"idempotency_key": "..."`` would trip
        the ``kv_secret`` detector on the field name alone, which says nothing
        about whether the canary carries a secret.
        """

        document = self.model_dump(mode="json", exclude_none=True)
        out: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, str):
                out.append(node)
            elif isinstance(node, dict):
                for item in node.values():
                    walk(item)
            elif isinstance(node, (list, tuple)):
                for item in node:
                    walk(item)

        walk(document)
        return tuple(out)

    def sensitive_findings(self, *, extra_terms: Iterable[str] = ()) -> dict[str, tuple[str, ...]]:
        """Map each unclean value to its findings. Empty dict means clean."""

        extra_terms = tuple(extra_terms)
        report: dict[str, tuple[str, ...]] = {}
        for value in self.scannable_values():
            findings = scan_sensitive(value, extra_terms=extra_terms)
            if findings:
                report[value] = findings
        return report

    # -- request builders ------------------------------------------------
    def add_arguments(self) -> dict[str, Any]:
        return {
            "content": self.add.content,
            "scope": self.scope,
            "kind": self.kind,
            "tags": list(self.tags),
            "idempotency_key": self.add.idempotency_key,
        }

    def replace_arguments(self, memory_id: str) -> dict[str, Any]:
        return {
            "memory_id": memory_id,
            "expected_revision": self.replace.expected_revision,
            "content": self.replace.content,
            "scope": self.scope,
            "kind": self.kind,
            "tags": list(self.tags),
            "idempotency_key": self.replace.idempotency_key,
        }

    def remove_arguments(self, memory_id: str) -> dict[str, Any]:
        return {
            "memory_id": memory_id,
            "scope": self.scope,
            "expected_revision": self.remove.expected_revision,
            "idempotency_key": self.remove.idempotency_key,
        }

    def add_request(self) -> models.AddRequest:
        return models.AddRequest(**self.add_arguments())

    def replace_request(self, memory_id: str) -> models.ReplaceRequest:
        return models.ReplaceRequest(**self.replace_arguments(memory_id))

    def remove_request(self, memory_id: str) -> models.RemoveRequest:
        return models.RemoveRequest(**self.remove_arguments(memory_id))


# --------------------------------------------------------------------------
# construction
# --------------------------------------------------------------------------


def new_run_id() -> str:
    """A fresh lowercase-hex run id containing at least one ``a-f``.

    The letter is not cosmetic: it is what lets `_HEX_DIGEST` recognise the id
    as a digest rather than as a numeric identifier.
    """

    while True:
        candidate = secrets.token_hex(RUN_ID_BYTES)
        if any(char in "abcdef" for char in candidate):
            return candidate


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_canary_plan(
    run_id: str | None = None,
    *,
    clock: Callable[[], str] | None = None,
) -> CanaryPlan:
    """Build one canary plan.

    ``run_id`` is generated when omitted; passing it makes the plan fully
    reproducible, which is how the artifact is regenerated for review.
    """

    if run_id is None:
        run_id = new_run_id()
    elif not _RUN_ID_RE.match(run_id):
        raise ValueError("run_id must be 12-32 lowercase hex characters")

    scope = f"{CANARY_SCOPE_PREFIX}{run_id}"
    if len(scope) > models.MAX_SCOPE_CHARS:
        raise ValueError("run_id makes the canary scope exceed the scope length limit")

    add_content = _ADD_TEMPLATE.format(run_id=run_id)
    replace_content = _REPLACE_TEMPLATE.format(run_id=run_id)

    plan = CanaryPlan(
        run_id=run_id,
        created_at=(clock or _utc_now)(),
        scope=scope,
        add=CanaryWrite(
            operation="add",
            task="8.2",
            idempotency_key=f"canary-{run_id}-add",
            content=add_content,
            content_sha256=_sha256(add_content),
            expected_revision_after=1,
        ),
        replace=CanaryWrite(
            operation="replace",
            task="8.4",
            idempotency_key=f"canary-{run_id}-replace",
            content=replace_content,
            content_sha256=_sha256(replace_content),
            expected_revision=1,
            expected_revision_after=2,
        ),
        remove=CanaryWrite(
            operation="remove",
            task="8.6",
            idempotency_key=f"canary-{run_id}-remove",
            expected_revision=2,
            expected_revision_after=3,
        ),
    )

    # Fail closed at construction: an unsafe canary must never reach an
    # artifact, a reviewer or a host UI.
    findings = plan.sensitive_findings()
    if findings:
        raise ValueError(f"canary plan is not safe to publish: {sorted(findings.values())}")
    return plan


__all__ = [
    "CANARY_KIND",
    "CANARY_SCHEMA_VERSION",
    "CANARY_SCOPE_PREFIX",
    "CANARY_TAGS",
    "RUN_ID_BYTES",
    "RUN_ID_PATTERN",
    "CanaryPlan",
    "CanaryWrite",
    "build_canary_plan",
    "new_run_id",
    "scan_sensitive",
]
