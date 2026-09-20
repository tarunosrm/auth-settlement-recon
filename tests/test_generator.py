import random

from authgen.clock import LiveClock
from authgen.generator import AuthGenerator
from authgen.ledger import Ledger
from authgen.producer import NullSink

CFG = {  # elevated anomaly rates so small samples actually contain them
    "seed": 7,
    "anomalies": {"duplicate_send_rate": 0.05,
                  "late_response_rate": 0.05,
                  "reversal_rate": 0.10},
}


def make(tmp_path, n=600):
    ledger = Ledger(tmp_path / "led.jsonl")
    gen = AuthGenerator(CFG, ledger, NullSink(), rng=random.Random(7), clock=LiveClock())
    for _ in range(n):
        gen.step()
    return list(ledger.iter_records())


def test_money_is_integer_minor_units(tmp_path):
    for r in make(tmp_path):
        assert isinstance(r["amount_minor"], int) and r["amount_minor"] >= 100


def test_duplicates_share_auth_id(tmp_path):
    recs = make(tmp_path)
    by_id = {}
    for r in recs:
        if r["event_kind"] == "AUTH":
            by_id.setdefault(r["auth_id"], []).append(r)
    dup_pairs = [v for v in by_id.values() if len(v) > 1]
    assert dup_pairs, "elevated dup rate should produce duplicates"
    for pair in dup_pairs:
        assert len(pair) == 2, "each dup event emits exactly one retransmission"
        assert len({p["rrn"] for p in pair}) == 1          # identical retransmission
        tagged = [p for p in pair if "DUPLICATE_SEND" in p["ground_truth"]]
        assert len(tagged) == 1, "tag marks the retransmission only"
        dup_rec = tagged[0]
        original = next(p for p in pair if "DUPLICATE_SEND" not in p["ground_truth"])
        assert dup_rec["duplicate_of_seq"] == original["seq"]   # link points at the original

def test_reversals_reference_real_auths(tmp_path):
    recs = make(tmp_path)
    auth_ids = {r["auth_id"] for r in recs if r["event_kind"] == "AUTH"}
    for r in recs:
        if r["event_kind"] == "REVERSAL":
            assert r["original_auth_id"] in auth_ids
            assert r["mti"] == "0410" and r["reversal_reason"]
            assert r["response_code"] == "00"  # reversals reverse an approval


def test_late_response_is_tagged_and_uncertain(tmp_path):
    recs = make(tmp_path)
    late = [r for r in recs if "LATE_RESPONSE_UNCERTAIN_SETTLEMENT" in r["ground_truth"]]
    assert late
    assert all(r["response_code"] in ("68", "91") for r in late)


def test_approval_rate_in_plausible_band(tmp_path):
    recs = make(tmp_path)
    auths = [r for r in recs if r["event_kind"] == "AUTH"]
    appr = sum(1 for r in auths if r["response_code"] == "00")
    rate = appr / len(auths)
    assert 0.75 < rate < 0.95  # expiry + declines + late-responses included


class RecordingSink:
    def __init__(self):
        self.messages = []
    def send(self, messages):
        self.messages.extend(messages)


def test_stream_payload_is_clean(tmp_path):
    """THE invariant: what a consumer receives must carry NO ground truth."""
    sink = RecordingSink()
    ledger = Ledger(tmp_path / "l.jsonl")
    gen = AuthGenerator(CFG, ledger, sink, rng=random.Random(7), clock=LiveClock())
    for _ in range(300):
        gen.step()
    assert sink.messages
    for m in sink.messages:              # stream side: clean
        assert "ground_truth" not in m
        assert "seq" not in m
    assert any("ground_truth" in r for r in ledger.iter_records())  # oracle intact


def test_duplicate_and_reversal_links_are_intact(tmp_path):
    """Ledger cross-reference links must point at real sequence numbers."""
    recs = make(tmp_path)
    seqs = {r["seq"] for r in recs}
    linked = 0
    for r in recs:
        for key in ("duplicate_of_seq", "reversal_of_seq"):
            if key in r:
                assert r[key] in seqs, f"{key}={r[key]} points at nothing"
                linked += 1
    assert linked, "elevated anomaly rates should produce linked events"