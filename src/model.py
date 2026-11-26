from dataclasses import dataclass
from enum import IntEnum
import struct

MAGIC = 0x4D425443
PROTOCOL = 4
COMMAND_WORDS = 32
STATUS_WORDS = 29
MAX_DISCOVERY = 96
SWD_RATES_KHZ = (100, 200, 400, 1000, 2000, 4000, 8000)
READ_SWD_RATES_KHZ = (400, 1000, 2000, 4000, 8000)
SAMPLE_RATES_HZ = (10, 20, 50, 80, 100, 150, 200)
DEFAULT_SWD_KHZ = 400
DEFAULT_SAMPLE_HZ = 80


class Op(IntEnum):
    STOP = 1
    SCAN = 2
    SELECT = 3
    START = 4
    TARGET = 5
    ZERO = 6
    READ = 7
    WRITE = 8
    SAVE = 9
    NATIVE_ZERO = 10
    TUNE = 11


class State(IntEnum):
    IDLE = 0
    SCANNING = 1
    STARTING = 2
    RUNNING = 3
    STOPPING = 4
    FAULT = 5


RESULT = (
    "完成", "参数或路由无效", "等待有效反馈", "固件忙，请稍后重试", "应答超时",
    "读回值与写入值不同", "达妙控制需先确认量程和驱动器模式",
    "CAN 总线故障", "上位机心跳中断", "电机报告故障", "发现 CAN ID 冲突",
    "达到温度上限", "超出角度软限位", "请先让电机失能并停稳", "领控零点已写入，请重上电并重新选中电机",
    "目标或限速超出驱动器量程",
)
STATE = ("待机", "扫描中", "等待使能回包", "运行中", "等待停止确认", "已停止 · 故障")


@dataclass(frozen=True)
class Board:
    name: str
    key: str
    number: int
    target: str
    id_register: int
    device_id: int
    ports: int
    sram: int
    ram_size: int


BOARDS = (
    Board("RM C 板 · F407", "f407", 407, "STM32F407IGHx", 0xE0042000, 0x413, 2, 0x20000000, 128 * 1024),
    Board("DM-MC02 · H723", "h723", 723, "STM32H723VGTx", 0x5C001000, 0x483, 3, 0x24000000, 320 * 1024),
)
BRANDS = ("RM", "达妙", "领控")
MODELS = (
    ("3508 / C620", "2006 / C610", "GM6020"),
    ("DM-J4310", "DM-J4340P", "DM-J4310P", "DM-J4340", "DM-J6006",
     "DM-J6248P", "DM-J8006", "DM-J8009", "DM-J10010L", "DM-J10010"),
    ("MF4005", "MF9025", "MF4010", "MF4015", "MF5005", "MF5010", "MF5015", "MF6010", "MF6015"),
)


@dataclass(frozen=True)
class Node:
    port: int
    id: int
    rx: int
    tx: int
    flags: int = 1
    tick: int = 0

    @property
    def slot(self):
        return (self.flags & 7) - 1

    @property
    def conflict(self):
        return bool(self.flags & 0x100)


def bits(value):
    return struct.unpack("<I", struct.pack("<f", value))[0]


def real(value):
    return struct.unpack("<f", struct.pack("<I", value))[0]


def command(op, port=1, brand=0, model=0, node=None, args=(), encoder=65536, limits=(), control=(), ramp=0.0):
    words = [0] * COMMAND_WORDS
    words[1:5] = [int(op), port, brand, model]
    if node is not None:
        words[2] = node.port
        words[5:8] = [node.id, node.tx, node.rx]
        words[18] = node.slot
    words[8:8 + len(args)] = args
    words[17] = encoder
    if limits:
        words[19:24] = limits
    if control:
        words[24:28] = control
    words[28] = bits(ramp)
    return words


@dataclass(frozen=True)
class Parameter:
    addr: int
    name: str
    kind: str
    lo: float = 0
    hi: float = 0

    @property
    def writable(self):
        return self.lo < self.hi


DM_PARAMS = (
    Parameter(10, "控制模式", "mode", 1, 3),
    Parameter(2, "驱动器过温阈值 [°C]", "float", 80, 199),
    Parameter(4, "加速度", "float", 0.001, 10000),
    Parameter(5, "减速度", "float", -10000, -0.001),
    Parameter(6, "最大速度", "float", 0.001, 10000),
    Parameter(9, "通信超时计数", "int", 10, 1000000),
    Parameter(25, "速度环 Kp", "float", 0, 10000),
    Parameter(26, "速度环 Ki", "float", 0, 10000),
    Parameter(27, "位置环 Kp", "float", 0, 10000),
    Parameter(28, "位置环 Ki", "float", 0, 10000),
    Parameter(24, "电流环带宽", "float", 100, 10000),
    Parameter(31, "速度环阻尼系数", "float", 1, 30),
    Parameter(32, "速度环滤波带宽", "float", 0.001, 499.999),
    Parameter(8, "电机 CAN ID", "hex"),
    Parameter(7, "反馈 Master ID", "hex"),
    Parameter(21, "位置量程 [rad]", "float"),
    Parameter(22, "速度量程 [rad/s]", "float"),
    Parameter(23, "转矩量程 [N·m]", "float"),
    Parameter(60, "母线电压 [V]", "float"),
    Parameter(14, "驱动固件版本", "int"),
)
LK_PARAMS = (
    Parameter(0x30, "电机内部 PID", "pid", 0, 255),
    Parameter(0x33, "加速度 [°/s²]", "int", 1, 1000000),
    Parameter(0x90, "编码器零偏 [计数]", "int", 0, 16383),
)
LK_PID_NAMES = ("位置环 Kp", "位置环 Ki", "速度环 Kp", "速度环 Ki", "电流环 Kp", "电流环 Ki")
