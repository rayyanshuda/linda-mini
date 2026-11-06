"""End-to-end tests against a live portal: normal run, each injected fault, crash/resume,
idempotent rerun, and the two-session invariant. Run: pytest -m e2e"""
import pytest

from .conftest import audit_counts
from .expected import EXPECTED, route_dict

pytestmark = pytest.mark.e2e

# Subset used for fault scenarios (fast): first fax gets the injected fault, includes an
# exception path (critical -> review) and the resend which depends on FX-201.
SUBSET = "FX-201,FX-202,FX-210"


def assert_outcomes(results, ids=None):
    for fid, (action, key, classification, source) in EXPECTED.items():
        if ids and fid not in ids:
            continue
        r = results[fid]
        assert r["action"] == action, (fid, r["action"], r["errors"])
        assert r["classification"] == classification, fid
        assert r["source_document_id"] == source, fid
        if action in ("routed", "needs_review"):
            sel = {k: r["selected"][k] for k in ("assigned_to", "document_type", "folder")}
            assert sel == route_dict(key), fid
            assert r["verification"]["status"] == "verified" and r["verification"]["match"], fid
        if action == "skipped_duplicate":
            assert r["duplicate_of"] == "FX-201" and not r["submit_attempts"]
        if action == "already_routed":
            assert r["verification"]["observed"]["document_type"] == "Imaging Report"


def assert_two_sessions_and_single_routes(oracle):
    downloads = {}
    for d in oracle["downloads"]:
        downloads.setdefault(d["fax_id"], []).append(d)
    created = {s["id"]: s["created_at"] for s in oracle["sessions"]}
    for f in oracle["faxes"]:
        if not f["routing_session"] or f["routing_session"] == "STAFF":
            continue
        dls = downloads[f["fax_id"]]
        # route was made in a session different from, and created after, a download session
        assert all(d["session_id"] != f["routing_session"] for d in dls)
        assert any(d["downloaded_at"] < created.get(f["routing_session"], float("inf")) for d in dls)
    counts = audit_counts(oracle)
    assert not [k for k in counts if k[0] == "two_session_rule_blocked"]
    assert all(v == 1 for (ev, _), v in counts.items() if ev == "route_committed")


def test_full_queue_normal_and_idempotent_rerun(make_env):
    portal, agent = make_env()
    cp = agent.run()
    assert cp.returncode == 0, cp.stderr[-2000:]
    res = agent.results()
    assert set(res) == set(EXPECTED)
    assert_outcomes(res)
    for r in res.values():
        if r["session_b_ids"]:
            assert not set(r["session_a_ids"]) & set(r["session_b_ids"])
    o1 = portal.oracle()
    assert_two_sessions_and_single_routes(o1)
    assert sum(1 for f in o1["faxes"] if f["routing_session"] and f["routing_session"] != "STAFF") == 10
    # FX-211 untouched, FX-210 never routed
    fx211 = next(f for f in o1["faxes"] if f["fax_id"] == "FX-211")
    assert fx211["routing_session"] == "STAFF"
    assert next(f for f in o1["faxes"] if f["fax_id"] == "FX-210")["routed_at"] is None

    # second run: no downloads, no routes, same results
    cp2 = agent.run()
    assert cp2.returncode == 0
    o2 = portal.oracle()
    assert len(o2["downloads"]) == len(o1["downloads"])
    assert [e for e in o2["audit_events"] if e["event"] == "route_committed"] == \
           [e for e in o1["audit_events"] if e["event"] == "route_committed"]
    assert_outcomes(agent.results())


def test_download_503_once_is_retried(make_env):
    portal, agent = make_env("download_503_once")
    cp = agent.run("--only", SUBSET)
    assert cp.returncode == 0, cp.stderr[-2000:]
    res = agent.results()
    assert_outcomes(res, SUBSET.split(","))
    r = res["FX-201"]
    assert any(e["step"] == "download" and "503" in e["error"] for e in r["errors"])
    assert any("retrying" in x["what"] for x in r["recovery"])
    assert r["pdf_sha256"]
    assert_two_sessions_and_single_routes(portal.oracle())


def test_session_expire_on_route_reauthenticates(make_env):
    portal, agent = make_env("session_expire_on_route_once")
    cp = agent.run("--only", SUBSET)
    assert cp.returncode == 0, cp.stderr[-2000:]
    res = agent.results()
    assert_outcomes(res, SUBSET.split(","))
    r = res["FX-201"]
    assert [a["outcome"] for a in r["submit_attempts"]] == ["login_redirect", "success"]
    assert len(r["session_b_ids"]) == 2  # re-authenticated inside session B
    assert any("re-authenticated" in x["what"] for x in r["recovery"])
    o = portal.oracle()
    assert audit_counts(o)[("session_expired", "FX-201")] == 1
    assert_two_sessions_and_single_routes(o)


def test_route_commit_then_503_is_not_resubmitted(make_env):
    portal, agent = make_env("route_commit_then_503_once")
    cp = agent.run("--only", SUBSET)
    assert cp.returncode == 0, cp.stderr[-2000:]
    res = agent.results()
    assert_outcomes(res, SUBSET.split(","))
    r = res["FX-201"]
    assert [a["outcome"] for a in r["submit_attempts"]] == ["gateway_error"]   # exactly one click
    assert r["verification"]["status"] == "verified"
    assert any("persisted UI state" in x["what"] for x in r["recovery"])
    o = portal.oracle()
    c = audit_counts(o)
    assert c[("response_lost", "FX-201")] == 1
    assert c[("repeated_route_blocked", "FX-201")] == 0   # never even tried a second submit
    assert_two_sessions_and_single_routes(o)


@pytest.mark.parametrize("point", ["after_submit_click", "before_submit_click", "after_download"])
def test_crash_and_resume(make_env, point):
    portal, agent = make_env()
    ids = "FX-201,FX-202,FX-203"
    cp = agent.run("--only", ids, env={"AGENT_CRASH_AT": f"{point}:FX-202"})
    assert cp.returncode == 97, cp.stderr[-2000:]
    st = agent.state()["FX-202"]
    expected_state = {"after_submit_click": "SUBMIT_INTENT", "before_submit_click": "SUBMIT_INTENT",
                      "after_download": "DOWNLOADED"}[point]
    assert st["state"] == expected_state
    dl_before = len(portal.oracle()["downloads"])

    cp2 = agent.run("--only", ids)
    assert cp2.returncode == 0, cp2.stderr[-2000:]
    res = agent.results()
    assert_outcomes(res, ids.split(","))
    r = res["FX-202"]
    assert any("resumed" in x["what"] for x in r["recovery"]) or point == "after_download"
    o = portal.oracle()
    assert_two_sessions_and_single_routes(o)
    if point == "after_submit_click":
        # route already committed before the crash: resume verifies, never clicks again
        assert len(r["submit_attempts"]) == 1
    if point == "after_download":
        # cached, hash-verified PDF reused: FX-202 not downloaded again
        assert sum(1 for d in o["downloads"] if d["fax_id"] == "FX-202") == 1
    assert len(o["downloads"]) >= dl_before
