from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "firmware"
BUNDLE = ROOT / "bundle"
SYMBOLS = {
    "BenchIdentity": ("identity", 16),
    "BenchCommand": ("command", 128),
    "BenchHeartbeat": ("heartbeat", 4),
    "BenchStatus": ("status", 116),
    "BenchDiscovery": ("discovery", 96 * 20),
}


def run(*args: str | Path) -> str:
    return subprocess.check_output([str(arg) for arg in args], cwd=ROOT, encoding="utf-8", errors="replace")


def build(board: str, folder: str, number: int) -> None:
    source = FIRMWARE / folder
    out = ROOT / "build" / board
    version = hashlib.sha256()
    for tree in [source, FIRMWARE / "common"]:
        for file in sorted(path for path in tree.rglob("*") if path.is_file()):
            version.update(file.relative_to(FIRMWARE).as_posix().encode())
            version.update(file.read_bytes())
    build_id = struct.unpack("<I", version.digest()[:4])[0]
    print(run("cmake", "-S", source, "-B", out, "-G", "Ninja",
              "-DCMAKE_BUILD_TYPE=Release", f"-DBENCH_BUILD_ID={build_id}"), end="")
    print(run("cmake", "--build", out, "--parallel", 4), end="")
    elf = out / "FIRMWARE.elf"
    addresses = {}
    for line in run("arm-none-eabi-nm", "--print-size", elf).splitlines():
        columns = line.split()
        if len(columns) == 4 and columns[3] in SYMBOLS:
            address, size, _, symbol = columns
            key, expected = SYMBOLS[symbol]
            if int(size, 16) != expected:
                raise ValueError(f"Wrong size for {symbol}: {size}")
            addresses[key] = int(address, 16)
    if len(addresses) != len(SYMBOLS):
        raise ValueError(f"Missing mailbox symbols in {elf}")
    BUNDLE.mkdir(exist_ok=True)
    image = BUNDLE / f"{board}.hex"
    shutil.copy2(out / "FIRMWARE.hex", image)
    shutil.copy2(elf, BUNDLE / f"{board}.elf")
    (BUNDLE / f"{board}.json").write_text(json.dumps({
        "board": number,
        "abi": 4,
        "build": build_id,
        "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        **addresses,
    }, indent=2) + "\n", encoding="utf-8")
    print(f"{image.name}: {build_id:08X}, probe mailbox verified")


if __name__ == "__main__":
    build("f407", "RM_C", 407)
    build("h723", "H723", 723)
