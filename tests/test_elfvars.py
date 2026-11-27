import os
from pathlib import Path
import struct
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from elfvars import CType, ElfImage, Variable, decode, encode, extract, guess_roles, manual_type, plan_blocks  # noqa: E402

FLOAT = manual_type("float")
I16 = manual_type("int16")
U8 = manual_type("uint8")


def image(variables):
    img = ElfImage.__new__(ElfImage)
    img.variables, img._index = variables, None
    return img


def pid_struct():
    return CType("struct", "struct pid", 16, members=[
        ("kp", 0, FLOAT, 0), ("ki", 4, FLOAT, 0), ("mode", 8, I16, 0), ("flag", 10, U8, 1), ("out", 12, FLOAT, 0)])


def test_decode_encode_roundtrip():
    assert decode(FLOAT, encode(FLOAT, 1.5)) == 1.5
    assert decode(I16, encode(I16, -300)) == -300
    assert encode(U8, 255) == b"\xff"
    with pytest.raises(ValueError):
        encode(U8, 256)
    assert decode(manual_type("bool"), b"\x01") is True


def test_children_struct_array_and_bitfield():
    pid = pid_struct()
    arr = CType("array", "array", 32, element=pid, dims=(2,))
    img = image([Variable("motors", 0x20000100, arr)])
    kids = img.children("motors", 0x20000100, arr)
    assert [k[0] for k in kids] == ["motors[0]", "motors[1]"] and kids[1][1] == 0x20000110
    members = img.children("motors[1]", 0x20000110, pid)
    assert ("motors[1].flag", 0x2000011A) == members[3][:2] and members[3][3] is False  # 位域不可监视
    leaves = [leaf.path for leaf in img.leaves()]
    assert "motors[1].out" in leaves and "motors[0].flag" not in leaves
    leaf = img.resolve("motors[1].ki")
    assert leaf.address == 0x20000114 and leaf.ctype.encoding == "float"
    assert img.resolve("motors[3].ki") is None and img.resolve("motors[0].nope") is None


def test_pointer_resolution_uses_live_read():
    pid = pid_struct()
    pointer = CType("pointer", "struct pid *", 4, "uint", element=pid)
    holder = CType("struct", "struct holder", 8, members=[("id", 0, I16, 0), ("ctrl", 4, pointer, 0)])
    img = image([Variable("axis", 0x20000000, holder)])
    memory = {0x20000004: 0x20000200}
    leaf = img.resolve("axis.ctrl->ki", read32=memory.__getitem__)
    assert leaf.address == 0x20000204
    assert img.resolve("axis.ctrl->ki") is None  # 没有读函数时无法解引用
    kids = img.deref_children("axis.ctrl", 0x20000200, pointer)
    assert kids[0][0] == "axis.ctrl->kp" and kids[0][1] == 0x20000200


def test_plan_blocks_merges_and_splits():
    items = [(0x20000008, 4), (0x20000000, 4), (0x20000013, 1), (0x20001000, 2)]
    blocks, placement = plan_blocks(items)
    assert blocks == [(0x20000000, 5), (0x20001000, 1)]
    assert placement == [(0, 8), (0, 0), (0, 0x13), (1, 0)]
    raw = [struct.pack("<5I", 1, 0, 2, 0, 0xAB000000), struct.pack("<I", 0xBEEF)]
    values = extract(raw, placement, items)
    assert values == [struct.pack("<I", 2), struct.pack("<I", 1), b"\xab", b"\xef\xbe"]
    big = [(0x20000000 + 4 * i, 4) for i in range(1000)]
    blocks, _ = plan_blocks(big)
    assert all(words <= 480 for _, words in blocks) and sum(w for _, w in blocks) == 1000


def test_guess_roles_common_styles():
    assert guess_roles(["target", "measure", "kp", "ki", "kd", "out", "integral_max", "out_max"]) == {
        "kp": "kp", "ki": "ki", "kd": "kd", "target": "target", "feedback": "measure", "output": "out",
        "max_out": "out_max", "max_iout": "integral_max"}
    roles = guess_roles(["Kp", "Ki", "Kd", "set", "fdb", "max_out", "max_iout", "out"])
    assert roles["target"] == "set" and roles["feedback"] == "fdb" and roles["max_iout"] == "max_iout"


ELFS = [p for p in (ROOT / "build" / "f407" / "FIRMWARE.elf",
                    Path(os.environ.get("MB_TEST_AXF", "__none__"))) if p.is_file()]


@pytest.mark.parametrize("path", ELFS, ids=[p.name for p in ELFS])
def test_real_image(path):
    img = ElfImage(path)
    assert img.has_dwarf and img.variables and img.flash_chunks
    assert all(not 0x08000000 <= v.address < 0x08200000 for v in img.variables)
    status = img.resolve("BenchStatus[3]") if path.name == "FIRMWARE.elf" else None
    if status is not None:
        assert status.size == 4
