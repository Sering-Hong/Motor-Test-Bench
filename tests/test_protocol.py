"""上位机与固件邮箱的一致性检查：常量、命令字布局、清单地址。"""
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from model import COMMAND_WORDS, MAGIC, PROTOCOL, STATUS_WORDS, Op, bits, command, real  # noqa: E402

MAILBOX = (ROOT / "firmware" / "common" / "mailbox.hpp").read_text(encoding="utf-8")


def constant(name):
    return int(re.search(rf"{name}\s*=\s*(0x[0-9A-Fa-f]+|\d+)", MAILBOX)[1], 0)


def test_constants_match_firmware():
    assert constant("Protocol") == PROTOCOL
    assert constant("Magic") == MAGIC
    assert constant("CommandWords") == COMMAND_WORDS
    assert constant("StatusWords") == STATUS_WORDS


def test_operation_numbers_match_firmware():
    names = re.search(r"enum Operation : uint32_t \{([^}]*)\}", MAILBOX)[1]
    items = [item.split("=")[0].strip() for item in names.split(",")]
    assert items[0] == "Stop" and len(items) == len(Op)
    assert items[-1] == "Tune" and Op.TUNE == len(items)


def test_command_layout():
    words = command(Op.TUNE, args=tuple(bits(v) for v in (1, 2, 3, 4, 5, 6, 7, 8)), ramp=500.0)
    assert len(words) == COMMAND_WORDS
    assert words[1] == Op.TUNE
    assert [real(w) for w in words[8:16]] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert real(words[28]) == 500.0
    assert command(Op.STOP)[28] == 0


def test_bundle_manifests_match_protocol():
    for board, number in (("f407", 407), ("h723", 723)):
        manifest = json.loads((ROOT / "bundle" / f"{board}.json").read_text(encoding="utf-8"))
        assert manifest["board"] == number and manifest["abi"] == PROTOCOL
