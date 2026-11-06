import json

from agent import ledger as L


def test_atomic_save_and_reload(tmp_path):
    led = L.Ledger(tmp_path)
    rec = led.get("FX-1")
    led.transition(rec, L.DOWNLOADED, download={"sha256": "x"})
    led2 = L.Ledger(tmp_path)
    assert led2.get("FX-1")["state"] == L.DOWNLOADED
    assert not list(tmp_path.glob(".state.*.tmp"))
    events = [json.loads(x) for x in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert events[-1]["to"] == L.DOWNLOADED


def test_submit_intent_survives_restart(tmp_path):
    led = L.Ledger(tmp_path)
    rec = led.get("FX-2")
    rec["submit_attempts"].append({"n": 1, "outcome": None})
    led.transition(rec, L.SUBMIT_INTENT)
    rec2 = L.Ledger(tmp_path).get("FX-2")
    assert rec2["state"] == L.SUBMIT_INTENT and rec2["submit_attempts"][0]["outcome"] is None
