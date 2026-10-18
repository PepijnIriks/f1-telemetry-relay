"""The F1 22 packet layouts must match the official UDP specification sizes."""
from f1_format import DAMAGE_STRUCT, HEADER, HEADER_FIELDS, LAP_STRUCT, STATUS_STRUCT, TEL_STRUCT


def test_struct_sizes():
    assert HEADER.size == 24
    assert TEL_STRUCT.size == 60
    assert LAP_STRUCT.size == 43
    assert STATUS_STRUCT.size == 47
    assert DAMAGE_STRUCT.size == 42


def test_header_of_recorded_packets(packets):
    for p in packets[:200]:
        h = dict(zip(HEADER_FIELDS, HEADER.unpack_from(p)))
        assert h["format"] == 2022
        assert 0 <= h["packet_id"] <= 11
        assert 0 <= h["player"] < 22
