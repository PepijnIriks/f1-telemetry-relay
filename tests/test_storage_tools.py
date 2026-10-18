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


def test_tidy_moves_old_places_into_data(tmp_path, monkeypatch):
    """Files of older versions (spool, exports, logs...) move into data/; nothing is overwritten or lost."""
    import paths
    old_spool, new_spool = tmp_path / "spool", tmp_path / "data" / "spool"
    old_log, new_log = tmp_path / "firewall.log", tmp_path / "data" / "firewall.log"
    old_spool.mkdir()
    (old_spool / "1-10.pkl").write_bytes(b"race data not in Azure yet")
    new_spool.mkdir(parents=True)
    (new_spool / "2-20.pkl").write_bytes(b"newer")
    old_log.write_text("old log")
    new_log.write_text("new log")                         # already there: the old one must not overwrite it
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "OLD_PLACES", [(old_spool, new_spool), (old_log, new_log),
                                              (tmp_path / "missing", tmp_path / "data" / "x")])
    assert paths.tidy(say=lambda *_: None) == 2
    assert (new_spool / "1-10.pkl").read_bytes() == b"race data not in Azure yet"
    assert (new_spool / "2-20.pkl").read_bytes() == b"newer"
    assert not old_spool.exists()                          # empty old folder removed
    assert new_log.read_text() == "new log"
    assert (tmp_path / "data" / "firewall-old.log").read_text() == "old log"
    assert paths.tidy(say=lambda *_: None) == 0            # second time: nothing to do


def test_read_config(tmp_path, monkeypatch):
    import paths
    f = tmp_path / "config.env"
    f.write_text('# comment\nDRIVERS=Pepijn,Mathijs\n  HTTP_PORT = 8000 \nNAME="quoted"\nnot a setting\n', encoding="utf-8")
    monkeypatch.setattr(paths, "CONFIG_FILE", f)
    cfg = paths.read_config({"HTTP_PORT": "1", "OTHER": "x"})
    assert cfg == {"HTTP_PORT": "8000", "OTHER": "x", "DRIVERS": "Pepijn,Mathijs", "NAME": "quoted"}
