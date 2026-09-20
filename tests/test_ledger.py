from authgen.ledger import Ledger


def test_roundtrip_and_sequence(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    s1 = led.append({"x": 1})
    s2 = led.append({"x": 2})
    assert s2 == s1 + 1
    recs = list(led.iter_records())
    assert [r["seq"] for r in recs] == [s1, s2]
    assert recs[1]["x"] == 2