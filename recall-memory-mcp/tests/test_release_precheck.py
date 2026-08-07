"""Task 10.9 — pre-release re-verification logic.

Task 10.9 says: immediately before a release, re-verify that the distribution
name is still what we claimed and that the official client / SDK contracts have
not changed; if they have, reopen Gate 3 / Gate 7.

The dangerous failure mode is not "the check says FAIL".  It is a check that
says PASS on incomplete evidence -- a source that timed out, a digest that was
never compared, or an evidence file that was written for an older commit and
then reused for a later release.  Every test below exists to make one of those
silent passes impossible.

This module is deliberately network-free: the live driver
(`scripts/phase10_9_release_precheck.py`) does the fetching and feeds probes in.
That split is what lets the fail-closed rules be tested at all.
"""

from __future__ import annotations

import json

import pytest

from recall_memory_mcp import release_precheck as rp

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

BASE_A = "a" * 64
BASE_B = "b" * 64


def baseline(source_id: str = "kiro", sha: str = BASE_A, size: int = 100) -> rp.SourceBaseline:
    return rp.SourceBaseline(
        source_id=source_id,
        url=f"https://example.invalid/{source_id}",
        sha256=sha,
        bytes=size,
    )


def probe(
    source_id: str = "kiro",
    *,
    status: int | None = 200,
    sha: str | None = BASE_A,
    size: int | None = 100,
    error: str | None = None,
) -> rp.SourceProbe:
    return rp.SourceProbe(
        source_id=source_id,
        url=f"https://example.invalid/{source_id}",
        http_status=status,
        bytes=size,
        sha256=sha,
        error=error,
    )


# --------------------------------------------------------------------------
# 1. per-source drift classification
# --------------------------------------------------------------------------


def test_identical_digest_is_unchanged() -> None:
    assert rp.classify_source(baseline(), probe()) == rp.DRIFT_UNCHANGED


def test_different_digest_is_digest_drift_not_unchanged() -> None:
    assert rp.classify_source(baseline(), probe(sha=BASE_B, size=222)) == rp.DRIFT_DIGEST


def test_transport_failure_is_unreachable_never_unchanged() -> None:
    verdict = rp.classify_source(baseline(), probe(status=None, sha=None, size=None, error="URLError"))
    assert verdict == rp.DRIFT_UNREACHABLE


def test_non_200_is_http_error_even_though_a_body_arrived() -> None:
    # A 404 body has a perfectly good digest.  It must not be compared as if it
    # were the document.
    assert rp.classify_source(baseline(), probe(status=404, sha=BASE_B, size=9)) == rp.DRIFT_HTTP_ERROR


def test_source_absent_from_baseline_is_new_not_unchanged() -> None:
    assert rp.classify_source(None, probe()) == rp.DRIFT_NEW


def test_missing_digest_on_a_200_is_unreachable_not_unchanged() -> None:
    # Defensive: a driver bug that forgets to hash must not read as "same".
    assert rp.classify_source(baseline(), probe(sha=None)) == rp.DRIFT_UNREACHABLE


# --------------------------------------------------------------------------
# 2. name checks
# --------------------------------------------------------------------------


def test_name_still_free_when_pypi_returns_404() -> None:
    expectation = rp.NameExpectation("pypi-json-ours", "https://pypi.org/pypi/x/json", (404,), "free")
    result = rp.evaluate_name(expectation, probe("pypi-json-ours", status=404))
    assert result.ok is True
    assert result.observed_status == 404


def test_name_taken_is_a_failure_not_a_warning() -> None:
    expectation = rp.NameExpectation("pypi-json-ours", "https://pypi.org/pypi/x/json", (404,), "free")
    result = rp.evaluate_name(expectation, probe("pypi-json-ours", status=200))
    assert result.ok is False
    assert "200" in result.detail


def test_occupied_neighbour_must_stay_occupied() -> None:
    # `recall-mcp` is a different project.  If it vanished we would need to
    # re-decide the naming story rather than silently inherit the name.
    expectation = rp.NameExpectation("pypi-json-other", "https://pypi.org/pypi/y/json", (200,), "occupied")
    assert rp.evaluate_name(expectation, probe("pypi-json-other", status=200)).ok is True
    assert rp.evaluate_name(expectation, probe("pypi-json-other", status=404)).ok is False


def test_unreachable_name_check_is_not_ok() -> None:
    expectation = rp.NameExpectation("pypi-json-ours", "https://pypi.org/pypi/x/json", (404,), "free")
    result = rp.evaluate_name(expectation, probe("pypi-json-ours", status=None, error="URLError"))
    assert result.ok is False
    assert result.observed_status is None


def test_unreachable_name_check_is_marked_unobserved() -> None:
    # "I could not reach the registry" and "the registry says the name is
    # taken" are different facts and must not collapse into one verdict.
    expectation = rp.NameExpectation("pypi-json-ours", "https://pypi.org/pypi/x/json", (404,), "free")
    unreachable = rp.evaluate_name(expectation, probe("pypi-json-ours", status=None, error="URLError"))
    taken = rp.evaluate_name(expectation, probe("pypi-json-ours", status=200))
    assert unreachable.observed is False
    assert taken.observed is True
    assert rp.evaluate_name(expectation, probe("pypi-json-ours", status=404)).observed is True


# --------------------------------------------------------------------------
# 3. SDK pin
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "version", "expected"),
    [
        ("mcp>=2.0,<2.1", "2.0.0", True),
        ("mcp>=2.0,<2.1", "2.0.7", True),
        ("mcp>=2.0,<2.1", "2.1.0", False),
        ("mcp>=2.0,<2.1", "3.0.0", False),
        ("mcp>=2.0,<2.1", "1.9.4", False),
        ("mcp>=2.0,<2.1", "2.1.0rc1", False),
    ],
)
def test_sdk_latest_against_pin(spec: str, version: str, expected: bool) -> None:
    assert rp.sdk_within_pin(version, spec) is expected


@pytest.mark.parametrize("version", ["2.0.1rc1", "2.0.1a1", "2.0.1b2", "2.0.1.dev1"])
def test_prerelease_inside_the_numeric_range_is_still_rejected(version: str) -> None:
    # The escape the mutation probe found: `2.1.0rc1` is already excluded by the
    # `<2.1` bound, so it cannot prove the pre-release rule exists.  A
    # pre-release that sits *inside* the range can.  pip will not install these
    # by default, so they are not what a user would get.
    assert rp.sdk_within_pin(version, "mcp>=2.0,<2.1") is False


@pytest.mark.parametrize("spec", ["", "mcp", "mcp latest", "   "])
def test_a_pin_with_no_parseable_constraint_accepts_nothing(spec: str) -> None:
    # Otherwise a typo in pyproject.toml silently turns the SDK check into a
    # rubber stamp that can never fail.
    assert rp.sdk_within_pin("2.0.0", spec) is False


def test_unparseable_version_is_not_silently_in_range() -> None:
    assert rp.sdk_within_pin("not-a-version", "mcp>=2.0,<2.1") is False


def test_unknown_latest_version_is_not_in_range() -> None:
    assert rp.sdk_within_pin(None, "mcp>=2.0,<2.1") is False


# --------------------------------------------------------------------------
# 4. overall verdict -- the fail-closed core
# --------------------------------------------------------------------------


def complete_ok_inputs() -> dict:
    return {
        "source_verdicts": {"a": rp.DRIFT_UNCHANGED, "b": rp.DRIFT_UNCHANGED},
        "name_results": [
            rp.NameResult("pypi-json-ours", True, 404, "free as expected"),
            rp.NameResult("pypi-json-other", True, 200, "occupied as expected"),
        ],
        "ungrounded_claims": [],
        "claims_checked": 12,
        "sdk_in_pin": True,
        "expected_sources": {"a", "b"},
    }


def test_a_fully_green_complete_run_passes() -> None:
    assert rp.overall_verdict(**complete_ok_inputs()) == rp.VERDICT_PASS


def test_one_unreachable_source_can_never_pass() -> None:
    args = complete_ok_inputs()
    args["source_verdicts"]["b"] = rp.DRIFT_UNREACHABLE
    assert rp.overall_verdict(**args) == rp.VERDICT_INCOMPLETE


def test_a_source_that_was_never_probed_can_never_pass() -> None:
    # The subtlest hole: drop a source from the run and every remaining check is
    # green.  Absence must be detected against the expected set, not the
    # results dict.
    args = complete_ok_inputs()
    del args["source_verdicts"]["b"]
    assert rp.overall_verdict(**args) == rp.VERDICT_INCOMPLETE


def test_zero_claims_checked_can_never_pass() -> None:
    args = complete_ok_inputs()
    args["claims_checked"] = 0
    assert rp.overall_verdict(**args) == rp.VERDICT_INCOMPLETE


def test_ungrounded_claim_is_fail() -> None:
    args = complete_ok_inputs()
    args["ungrounded_claims"] = ["kiro-user-config-path: not found in vendor doc"]
    assert rp.overall_verdict(**args) == rp.VERDICT_FAIL


def test_name_violation_is_fail() -> None:
    args = complete_ok_inputs()
    args["name_results"][0] = rp.NameResult("pypi-json-ours", False, 200, "taken")
    assert rp.overall_verdict(**args) == rp.VERDICT_FAIL


def test_unreachable_name_check_is_incomplete_not_fail() -> None:
    # A registry timeout is missing evidence, not a naming violation.  Calling
    # it FAIL would be just as dishonest as calling it PASS.
    args = complete_ok_inputs()
    args["name_results"][0] = rp.NameResult(
        "pypi-json-ours", False, None, "unreachable", observed=False
    )
    assert rp.overall_verdict(**args) == rp.VERDICT_INCOMPLETE


def test_unreachable_name_check_still_cannot_pass() -> None:
    args = complete_ok_inputs()
    args["name_results"][0] = rp.NameResult(
        "pypi-json-ours", False, None, "unreachable", observed=False
    )
    assert rp.overall_verdict(**args) != rp.VERDICT_PASS


def test_observed_name_violation_beats_an_unrelated_registry_timeout() -> None:
    args = complete_ok_inputs()
    args["name_results"][0] = rp.NameResult("pypi-json-ours", False, 200, "taken")
    args["name_results"][1] = rp.NameResult(
        "pypi-json-other", False, None, "unreachable", observed=False
    )
    assert rp.overall_verdict(**args) == rp.VERDICT_FAIL


def test_sdk_out_of_pin_is_fail() -> None:
    args = complete_ok_inputs()
    args["sdk_in_pin"] = False
    assert rp.overall_verdict(**args) == rp.VERDICT_FAIL


def test_digest_drift_alone_is_not_a_failure_when_claims_still_ground() -> None:
    # Vendor docs get re-rendered constantly.  A digest change is a signal to
    # re-ground the claims, not a release blocker by itself.
    args = complete_ok_inputs()
    args["source_verdicts"]["a"] = rp.DRIFT_DIGEST
    assert rp.overall_verdict(**args) == rp.VERDICT_PASS


def test_digest_drift_with_an_ungrounded_claim_is_fail() -> None:
    args = complete_ok_inputs()
    args["source_verdicts"]["a"] = rp.DRIFT_DIGEST
    args["ungrounded_claims"] = ["vscode-add-mcp-flag: gone"]
    assert rp.overall_verdict(**args) == rp.VERDICT_FAIL


def test_fail_dominates_incomplete() -> None:
    # If a real contract break is visible we must say FAIL, not hide it behind
    # "we could not finish".
    args = complete_ok_inputs()
    args["source_verdicts"]["b"] = rp.DRIFT_UNREACHABLE
    args["ungrounded_claims"] = ["something: gone"]
    assert rp.overall_verdict(**args) == rp.VERDICT_FAIL


def test_http_error_source_can_never_pass() -> None:
    args = complete_ok_inputs()
    args["source_verdicts"]["b"] = rp.DRIFT_HTTP_ERROR
    assert rp.overall_verdict(**args) == rp.VERDICT_INCOMPLETE


def test_only_pass_is_tickable() -> None:
    assert rp.is_tickable(rp.VERDICT_PASS) is True
    assert rp.is_tickable(rp.VERDICT_INCOMPLETE) is False
    assert rp.is_tickable(rp.VERDICT_FAIL) is False


def test_reopened_gates_are_named_for_the_thing_that_broke() -> None:
    assert rp.reopened_gates(sdk_in_pin=False, ungrounded_claims=[]) == ("Gate 3",)
    assert rp.reopened_gates(sdk_in_pin=True, ungrounded_claims=["x"]) == ("Gate 7",)
    assert rp.reopened_gates(sdk_in_pin=False, ungrounded_claims=["x"]) == ("Gate 3", "Gate 7")
    assert rp.reopened_gates(sdk_in_pin=True, ungrounded_claims=[]) == ()


# --------------------------------------------------------------------------
# 5. evidence expiry -- 10.9 is only valid for the release it precedes
# --------------------------------------------------------------------------

COMMIT_A = "ad1f08651e1cbe93641aceb6f42fa2e831b4de6e"
COMMIT_B = "0123456789abcdef0123456789abcdef01234567"


def make_report(**overrides) -> rp.PrecheckReport:
    payload = {
        "generated_at": "2026-08-07T02:00:00+00:00",
        "head_commit": COMMIT_A,
        "verdict": rp.VERDICT_PASS,
        "validity_hours": 24,
        "sources": [],
        "name_results": [],
        "claims_checked": 12,
        "ungrounded_claims": [],
        "sdk_latest": "2.0.0",
        "sdk_pin": "mcp>=2.0,<2.1",
        "sdk_in_pin": True,
        "reopened_gates": (),
    }
    payload.update(overrides)
    return rp.PrecheckReport(**payload)


def test_report_is_valid_for_the_commit_it_was_generated_on() -> None:
    report = make_report()
    assert report.is_valid_for(COMMIT_A, now="2026-08-07T03:00:00+00:00") is True


def test_report_is_invalid_for_a_later_commit() -> None:
    # Code changed after the precheck -> the precheck no longer describes what
    # is about to be published.
    report = make_report()
    assert report.is_valid_for(COMMIT_B, now="2026-08-07T03:00:00+00:00") is False


def test_report_expires() -> None:
    report = make_report()
    assert report.is_valid_for(COMMIT_A, now="2026-08-09T03:00:00+00:00") is False


def test_a_non_pass_report_is_never_valid_for_a_release() -> None:
    report = make_report(verdict=rp.VERDICT_INCOMPLETE)
    assert report.is_valid_for(COMMIT_A, now="2026-08-07T03:00:00+00:00") is False


def test_expires_at_is_derived_not_free_text() -> None:
    report = make_report(generated_at="2026-08-07T02:00:00+00:00", validity_hours=24)
    assert report.expires_at() == "2026-08-08T02:00:00+00:00"


# --------------------------------------------------------------------------
# 6. rendering / serialisation
# --------------------------------------------------------------------------


def test_render_states_the_verdict_and_the_expiry() -> None:
    text = make_report().render()
    assert rp.VERDICT_PASS in text
    assert "expires_at" in text
    assert COMMIT_A in text


def test_render_of_an_incomplete_run_says_it_is_not_tickable() -> None:
    text = make_report(verdict=rp.VERDICT_INCOMPLETE).render()
    assert "NOT tickable" in text


def test_render_distinguishes_an_unseen_registry_from_a_failed_one() -> None:
    text = make_report(
        verdict=rp.VERDICT_INCOMPLETE,
        name_results=[
            {"id": "gh", "ok": False, "observed": False, "detail": "unreachable"},
            {"id": "pypi", "ok": False, "observed": True, "detail": "taken"},
        ],
    ).render()
    assert "[UNSEEN]" in text
    assert "[FAIL  ]" in text


def test_json_payload_round_trips() -> None:
    report = make_report()
    payload = json.loads(json.dumps(report.to_json()))
    assert payload["verdict"] == rp.VERDICT_PASS
    assert payload["head_commit"] == COMMIT_A
    assert payload["expires_at"] == "2026-08-08T02:00:00+00:00"


def test_report_records_every_source_url_and_status() -> None:
    report = make_report(
        sources=[
            {
                "id": "kiro",
                "url": "https://kiro.dev/docs/mcp/configuration.md",
                "http_status": None,
                "verdict": rp.DRIFT_UNREACHABLE,
                "error": "URLError",
            }
        ],
        verdict=rp.VERDICT_INCOMPLETE,
    )
    payload = report.to_json()
    assert payload["sources"][0]["url"].startswith("https://kiro.dev/")
    assert payload["sources"][0]["verdict"] == rp.DRIFT_UNREACHABLE


def test_no_secret_material_in_render() -> None:
    text = make_report().render().lower()
    for forbidden in ("token", "authorization", "bearer", "api_key", "password"):
        assert forbidden not in text


# --------------------------------------------------------------------------
# 7. baseline loading
# --------------------------------------------------------------------------


def test_load_baselines_from_phase7_doc_sources_shape() -> None:
    raw = {
        "generated_at": "2026-08-05T08:47:26+00:00",
        "sources": [
            {"id": "kiro", "url": "https://k/", "sha256": BASE_A, "bytes": 10, "http_status": 200},
            {"id": "cursor", "url": "https://c/", "sha256": BASE_B, "bytes": 20, "http_status": 200},
        ],
    }
    baselines = rp.load_baselines(raw)
    assert set(baselines) == {"kiro", "cursor"}
    assert baselines["kiro"].sha256 == BASE_A


def test_baseline_entry_without_a_digest_is_dropped_not_treated_as_empty() -> None:
    raw = {"sources": [{"id": "dead", "url": "https://d/", "http_status": None, "error": "URLError"}]}
    assert rp.load_baselines(raw) == {}
