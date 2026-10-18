"""Safety net (spool) and the driver list."""
import spool
from drivers import DriverList


def test_spool_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(spool, "DIR", tmp_path / "spool")
    b = spool.empty_batch()
    assert spool.is_empty(b) and spool.save(b) is None      # nothing to save
    b["pkts"] = [(1.0, "Pepijn", "1", 6, 1, 0.1, 0, b"\x01\x02", "r1")] * 3
    b["metas"]["r1"] = {"uid": "1"}
    p = spool.save(b)
    assert spool.files() == [p] and spool.count() == 3
    assert spool.load(p) == b
    spool.done(p)
    assert spool.files() == [] and spool.count() == 0


def test_spool_merge_keeps_order():
    older, newer = spool.empty_batch(), spool.empty_batch()
    older["pkts"], newer["pkts"] = [1, 2], [3]
    spool.merge(newer, older)                                # older data goes first
    assert newer["pkts"] == [1, 2, 3]


def test_driver_list(tmp_path, monkeypatch):
    import drivers
    monkeypatch.setattr(drivers, "FILE", tmp_path / "drivers.json")
    d = DriverList({"DRIVERS": "Pepijn, Mathijs"})
    assert d.names == ["Pepijn", "Mathijs"]
    assert d.resolve("2") == "Mathijs"                       # by number
    assert d.resolve("pepijn") == "Pepijn"                   # spelling from the list
    assert d.resolve("  Lotte  ") == "Lotte" and "Lotte" in d.names     # new name is added and remembered
    assert DriverList({"DRIVERS": ""}).names == ["Pepijn", "Mathijs", "Lotte"]
    d.remove("lotte")
    assert d.names == ["Pepijn", "Mathijs"]
    assert d.resolve("") == ""
