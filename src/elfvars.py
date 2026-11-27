"""从固件 ELF / Keil AXF 的 DWARF 调试信息解析全局变量、结构体成员和数组，供实车调参监视与写入。

- 支持 GCC（DWARF 2~5）和 ARMCC 5（DWARF 3）；成员偏移兼容常量与 DW_OP_plus_uconst 两种写法。
- 只收录有固定地址（DW_OP_addr）的变量，包括函数内的 static 变量；寄存器/栈上的局部变量没有固定地址，无法监视。
- 没有调试信息时退回符号表：只有名字、地址和大小，类型需手动指定。
- 位域成员不支持读写（标记为不可监视）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
import struct

FLASH_LO, FLASH_HI = 0x08000000, 0x08200000

BASE_TYPES = {  # 手动指定类型时的选项：名称 -> (编码, 字节数)
    "float": ("float", 4), "double": ("float", 8), "int8": ("int", 1), "uint8": ("uint", 1),
    "int16": ("int", 2), "uint16": ("uint", 2), "int32": ("int", 4), "uint32": ("uint", 4),
    "int64": ("int", 8), "uint64": ("uint", 8), "bool": ("bool", 1),
}
_ATE = {0x01: "uint", 0x02: "bool", 0x04: "float", 0x05: "int", 0x06: "int", 0x07: "uint", 0x08: "uint", 0x10: "uint"}


@dataclass
class CType:
    kind: str                 # base / struct / union / array / enum / pointer / unknown
    name: str
    size: int
    encoding: str = ""        # base/enum/pointer：float / int / uint / bool
    members: list = field(default_factory=list)   # [(名称, 偏移, CType, 位宽或 0)]
    element: "CType | None" = None
    dims: tuple = ()          # 数组各维长度
    enumerators: dict = field(default_factory=dict)

    @property
    def leaf(self):
        return self.kind in ("base", "enum", "pointer") and self.size in (1, 2, 4, 8)

    @property
    def display(self):
        if self.kind == "array":
            return f"{self.element.display}" + "".join(f"[{d}]" for d in self.dims)
        return self.name


UNKNOWN = CType("unknown", "?", 0)


@dataclass(frozen=True)
class Variable:
    name: str           # 显示名：全局变量为 name，函数内 static 为 函数::name
    address: int
    ctype: CType
    source: str = ""    # 所在源文件

    def path(self):
        return self.name


@dataclass(frozen=True)
class Leaf:
    """可监视的标量：完整路径、地址和类型。"""
    path: str
    address: int
    ctype: CType

    @property
    def size(self):
        return self.ctype.size

    @property
    def encoding(self):
        return self.ctype.encoding


def decode(ctype: CType, raw: bytes):
    enc, size = ctype.encoding, ctype.size
    if enc == "float":
        return struct.unpack("<f" if size == 4 else "<d", raw[:size])[0]
    signed = enc == "int"
    value = int.from_bytes(raw[:size], "little", signed=signed)
    return bool(value) if enc == "bool" else value


def encode(ctype: CType, value) -> bytes:
    enc, size = ctype.encoding, ctype.size
    if enc == "float":
        return struct.pack("<f" if size == 4 else "<d", float(value))
    number = int(round(float(value))) if not isinstance(value, bool) else int(value)
    lo, hi = (-(1 << (8 * size - 1)), (1 << (8 * size - 1)) - 1) if enc == "int" else (0, (1 << (8 * size)) - 1)
    if not lo <= number <= hi:
        raise ValueError(f"{value} 超出 {ctype.name} 范围 {lo}~{hi}")
    return number.to_bytes(size, "little", signed=enc == "int")


def manual_type(name):
    encoding, size = BASE_TYPES[name]
    return CType("base", name, size, encoding)


class ElfImage:
    """解析后的固件镜像。variables 为顶层变量；leaves() 展开成员。"""

    def __init__(self, path):
        from elftools.elf.elffile import ELFFile
        from elftools.elf.sections import SymbolTableSection

        self.path = os.path.abspath(path)
        self.mtime = os.path.getmtime(self.path)
        self.variables: list[Variable] = []
        self.has_dwarf = False
        self.flash_chunks = []
        self._types = {}
        with open(self.path, "rb") as stream:
            elf = ELFFile(stream)
            if elf.header.e_machine != "EM_ARM" or not elf.little_endian:
                raise ValueError("请选择 ARM 小端固件（.elf / .axf）")
            self._load_flash(elf)
            seen = set()
            if elf.has_dwarf_info():
                self.has_dwarf = True
                self._load_dwarf(elf.get_dwarf_info(), seen)
            for section in elf.iter_sections():
                if isinstance(section, SymbolTableSection):
                    for symbol in section.iter_symbols():
                        entry = symbol.entry
                        if entry.st_info.type != "STT_OBJECT" or not symbol.name or entry.st_size <= 0:
                            continue
                        address = entry.st_value
                        if FLASH_LO <= address < FLASH_HI or address in seen:
                            continue
                        seen.add(address)
                        ctype = CType("unknown", f"{entry.st_size} 字节", entry.st_size)
                        self.variables.append(Variable(symbol.name, address, ctype, "符号表"))
        self.variables.sort(key=lambda v: v.name.lower())
        self._index = None

    # ---------------- Flash 比对 ----------------
    def _load_flash(self, elf):
        segments = [(seg["p_paddr"], seg.data()) for seg in elf.iter_segments()
                    if seg["p_type"] == "PT_LOAD" and seg["p_filesz"] and FLASH_LO <= seg["p_paddr"] < FLASH_HI]
        chunks = []
        for start, data in sorted(segments, key=lambda s: -len(s[1]))[:2]:
            for offset in (0, len(data) // 2, max(0, len(data) - 256)):
                piece = data[offset - offset % 4: offset - offset % 4 + 256]
                piece = piece[:len(piece) - len(piece) % 4]
                if piece:
                    chunks.append((start + offset - offset % 4, piece))
        self.flash_chunks = chunks

    # ---------------- DWARF ----------------
    def _load_dwarf(self, dwarf, seen):
        for cu in dwarf.iter_CUs():
            top = cu.get_top_DIE()
            source = os.path.basename(_text(top.attributes.get("DW_AT_name")) or "")
            for die in top.iter_children():
                if die.tag == "DW_TAG_variable":
                    self._add_variable(die, source, "", seen)
                elif die.tag == "DW_TAG_subprogram":
                    scope = _text(die.attributes.get("DW_AT_name")) or "?"
                    for child in _walk(die):
                        if child.tag == "DW_TAG_variable":
                            self._add_variable(child, source, scope, seen)

    def _add_variable(self, die, source, scope, seen):
        location = die.attributes.get("DW_AT_location")
        if location is None or not isinstance(location.value, list) or len(location.value) < 5:
            return
        if location.value[0] != 0x03:  # DW_OP_addr
            return
        address = int.from_bytes(bytes(location.value[1:5]), "little")
        spec = die
        for attr in ("DW_AT_specification", "DW_AT_abstract_origin"):
            if attr in die.attributes:
                spec = die.get_DIE_from_attribute(attr)
        name = _text(die.attributes.get("DW_AT_name")) or _text(spec.attributes.get("DW_AT_name"))
        if not name or address in seen or FLASH_LO <= address < FLASH_HI:
            return
        seen.add(address)
        type_die = die if "DW_AT_type" in die.attributes else spec
        ctype = self._type(type_die.get_DIE_from_attribute("DW_AT_type")) if "DW_AT_type" in type_die.attributes else UNKNOWN
        self.variables.append(Variable(f"{scope}::{name}" if scope else name, address, ctype, source))

    def _type(self, die, depth=0):
        key = die.offset
        if key in self._types:
            return self._types[key]
        tag = die.tag
        name = _text(die.attributes.get("DW_AT_name"))
        size = _int(die.attributes.get("DW_AT_byte_size"))
        if tag in ("DW_TAG_typedef", "DW_TAG_const_type", "DW_TAG_volatile_type", "DW_TAG_restrict_type",
                   "DW_TAG_atomic_type"):
            inner = self._type(die.get_DIE_from_attribute("DW_AT_type"), depth) if "DW_AT_type" in die.attributes \
                else CType("base", "void", 0)
            if tag == "DW_TAG_typedef" and name:
                inner = _renamed(inner, name)
            self._types[key] = inner
            return inner
        if tag == "DW_TAG_base_type":
            encoding = _ATE.get(_int(die.attributes.get("DW_AT_encoding")), "uint")
            result = CType("base", name or "?", size or 0, encoding)
        elif tag == "DW_TAG_pointer_type" or tag == "DW_TAG_reference_type":
            target, pointee = "void", None
            if "DW_AT_type" in die.attributes:
                target_die = die.get_DIE_from_attribute("DW_AT_type")
                target = _type_name(target_die)
                result = CType("pointer", f"{target} *", size or 4, "uint")
                self._types[key] = result  # 先登记，防止链表等自引用
                if depth < 24:
                    pointee = self._type(target_die, depth + 1)
                result.element = pointee if pointee is not None and pointee.size else None
                return result
            result = CType("pointer", f"{target} *", size or 4, "uint")
        elif tag == "DW_TAG_enumeration_type":
            result = CType("enum", f"enum {name}" if name else "enum", size or 4, "int")
            for child in die.iter_children():
                if child.tag == "DW_TAG_enumerator":
                    result.enumerators[_int(child.attributes.get("DW_AT_const_value"))] = _text(child.attributes.get("DW_AT_name"))
        elif tag in ("DW_TAG_structure_type", "DW_TAG_union_type", "DW_TAG_class_type"):
            kind = "union" if tag == "DW_TAG_union_type" else "struct"
            result = CType(kind, f"{kind} {name}" if name else kind, size or 0)
            self._types[key] = result  # 先登记，防止自引用
            if depth < 24:
                for child in die.iter_children():
                    if child.tag != "DW_TAG_member" or "DW_AT_type" not in child.attributes:
                        continue
                    offset = _member_offset(child.attributes.get("DW_AT_data_member_location"))
                    bits = _int(child.attributes.get("DW_AT_bit_size"))
                    member = self._type(child.get_DIE_from_attribute("DW_AT_type"), depth + 1)
                    result.members.append((_text(child.attributes.get("DW_AT_name")) or "<匿名>", offset, member, bits))
            return result
        elif tag == "DW_TAG_array_type":
            element = self._type(die.get_DIE_from_attribute("DW_AT_type"), depth + 1) if "DW_AT_type" in die.attributes else UNKNOWN
            dims = []
            for child in die.iter_children():
                if child.tag == "DW_TAG_subrange_type":
                    if "DW_AT_count" in child.attributes:
                        dims.append(_int(child.attributes["DW_AT_count"]))
                    elif "DW_AT_upper_bound" in child.attributes:
                        dims.append(_int(child.attributes["DW_AT_upper_bound"]) + 1)
                    else:
                        dims.append(0)
            dims = tuple(dims) or (0,)
            total = element.size
            for d in dims:
                total *= d
            result = CType("array", "array", size or total, element=element, dims=dims)
        else:
            result = CType("unknown", name or tag.replace("DW_TAG_", ""), size or 0)
        self._types[key] = result
        return result

    # ---------------- 展开与查找 ----------------
    @staticmethod
    def children(path, address, ctype, max_elements=256):
        """返回直接子项 [(路径, 地址, 类型, 可监视)]。"""
        out = []
        if ctype.kind in ("struct", "union"):
            for name, offset, member, bits in ctype.members:
                out.append((f"{path}.{name}", address + offset, member, member.leaf and not bits))
        elif ctype.kind == "array" and ctype.dims and ctype.element is not None:
            first, rest = ctype.dims[0], ctype.dims[1:]
            if rest:
                sub = CType("array", "array", ctype.size // max(1, first), element=ctype.element, dims=rest)
            else:
                sub = ctype.element
            stride = sub.size
            for i in range(min(first, max_elements)):
                out.append((f"{path}[{i}]", address + i * stride, sub, sub.leaf))
        return out

    def leaves(self, limit=300_000, max_elements=32, max_depth=8):
        """展开为全部可监视标量，用于搜索。大数组只展开前 max_elements 个。"""
        if self._index is not None:
            return self._index
        out = []

        def walk(path, address, ctype, depth):
            if len(out) >= limit:
                return
            if ctype.leaf:
                out.append(Leaf(path, address, ctype))
                return
            if depth >= max_depth:
                return
            for child_path, child_address, child_type, ok in self.children(path, address, ctype, max_elements):
                if ok or child_type.kind in ("struct", "union", "array"):
                    walk(child_path, child_address, child_type, depth + 1)

        for variable in self.variables:
            walk(variable.name, variable.address, variable.ctype, 0)
        self._index = out
        return out

    @staticmethod
    def deref_children(path, pointer_value, ctype, max_elements=256):
        """指针解引用后的子项。pointer_value 需从目标板实时读取。"""
        pointee = ctype.element
        if pointee is None or not pointer_value:
            return []
        if pointee.leaf:
            return [(f"*{path}", pointer_value, pointee, True)]
        return [(f"{path}->{child[0][len(path) + 1:]}" if child[0].startswith(path + ".") else child[0],
                 *child[1:]) for child in ElfImage.children(path, pointer_value, pointee, max_elements)]

    def resolve(self, path, read32=None):
        """按 a.b[2].c / a.p->c 形式的路径查找标量，用于重新编译后按名字重新定位。
        路径含 -> 时需要 read32(地址) 实时读取指针。"""
        deref_all = path.startswith("*")
        parts = [p for p in re.split(r"(->|\.|\[\d+\])", path.lstrip("*")) if p]
        if not parts:
            return None
        variable = next((v for v in self.variables if v.name == parts[0]), None)
        if variable is None:
            return None
        address, ctype = variable.address, variable.ctype
        i = 1
        while i < len(parts):
            token = parts[i]
            if token.startswith("["):
                if ctype.kind != "array":
                    return None
                index = int(token[1:-1])
                children = self.children("", address, ctype, max_elements=index + 1)
                if index >= len(children):
                    return None
                address, ctype = children[index][1], children[index][2]
                i += 1
                continue
            if token in (".", "->") and i + 1 < len(parts):
                if token == "->":
                    if ctype.kind != "pointer" or ctype.element is None or read32 is None:
                        return None
                    address, ctype = read32(address), ctype.element
                member = next((m for m in ctype.members if m[0] == parts[i + 1]), None)                     if ctype.kind in ("struct", "union") else None
                if member is None or member[3]:
                    return None
                address, ctype = address + member[1], member[2]
                i += 2
                continue
            return None
        if deref_all:
            if ctype.kind != "pointer" or ctype.element is None or read32 is None:
                return None
            address, ctype = read32(address), ctype.element
        return Leaf(path, address, ctype) if ctype.leaf else None

    def search(self, text, limit=500):
        text = text.strip().lower()
        if not text:
            return []
        words = text.split()
        return [leaf for leaf in self.leaves() if all(w in leaf.path.lower() for w in words)][:limit]

    def struct_leaves(self, path, address, ctype):
        """结构体的直接标量成员 {成员名: Leaf}，用于自动识别 PID 字段。"""
        out = {}
        for child_path, child_address, child_type, ok in self.children(path, address, ctype):
            if ok:
                out[child_path.rsplit(".", 1)[-1]] = Leaf(child_path, child_address, child_type)
        return out


# ---------------- 监视读取规划 ----------------
def plan_blocks(items, gap=32, max_words=480):
    """把若干 (地址, 字节数) 合并为对齐的读取块。返回 (blocks, placement)：
    blocks = [(起始地址, 字数)]，placement[i] = (块序号, 块内字节偏移)。"""
    order = sorted(range(len(items)), key=lambda i: items[i][0])
    blocks, placement = [], [None] * len(items)
    start = end = None
    members = []

    def flush():
        if start is not None:
            index = len(blocks)
            blocks.append((start, (end - start) // 4))
            for i in members:
                placement[i] = (index, items[i][0] - start)

    for i in order:
        address, size = items[i]
        lo, hi = address & ~3, (address + size + 3) & ~3
        if start is not None and lo - end <= gap and (max(hi, end) - start) // 4 <= max_words:
            end = max(end, hi)
            members.append(i)
        else:
            flush()
            start, end, members = lo, hi, [i]
    flush()
    return blocks, placement


def extract(block_bytes, placement, items):
    """按 plan_blocks 的结果从各块字节中取出每项原始字节。"""
    return [block_bytes[b][offset:offset + items[i][1]] for i, (b, offset) in enumerate(placement)]


# ---------------- 自动识别 PID 结构体 ----------------
ROLE_PATTERNS = (
    ("kp", r"^(k_?p|p|p_?gain|kp_?\w*)$"),
    ("ki", r"^(k_?i|i|i_?gain|ki_?\w*)$"),
    ("kd", r"^(k_?d|d|d_?gain|kd_?\w*)$"),
    ("target", r"^(set|set_?point|target|tar|ref|reference|expect\w*|goal|setpoint)$"),
    ("feedback", r"^(fdb|feedback|measure\w*|get|now|cur(rent)?|actual|real|fb|meas)$"),
    ("output", r"^(out|output|result|u)$"),
    ("max_out", r"^(max_?out\w*|out_?max|output_?limit|limit_?out|out_?limit)$"),
    ("max_iout", r"^(max_?i_?out|i_?out_?max|integral_?(limit|max)|i_?limit|max_?integral|iout_?max)$"),
)


def guess_roles(names):
    """按成员名猜 PID 角色：返回 {角色: 成员名}。"""
    roles = {}
    for role, pattern in ROLE_PATTERNS:
        regex = re.compile(pattern, re.IGNORECASE)
        for name in names:
            if regex.match(name) and name not in roles.values():
                roles[role] = name
                break
    return roles


# ---------------- 工具 ----------------
def _text(attr):
    if attr is None:
        return ""
    value = attr.value
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _int(attr):
    if attr is None:
        return 0
    value = attr.value
    if isinstance(value, list):
        return _member_offset(attr)
    return int(value)


def _member_offset(attr):
    if attr is None:
        return 0
    value = attr.value
    if isinstance(value, int):
        return value
    if isinstance(value, list) and value and value[0] == 0x23:  # DW_OP_plus_uconst + ULEB128
        result, shift = 0, 0
        for byte in value[1:]:
            result |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
        return result
    return 0


def _walk(die):
    for child in die.iter_children():
        yield child
        if child.tag in ("DW_TAG_lexical_block", "DW_TAG_inlined_subroutine"):
            yield from _walk(child)


def _is_const(die, spec):
    for d in (die, spec):
        current = d
        for _ in range(4):
            if "DW_AT_type" not in current.attributes:
                break
            current = current.get_DIE_from_attribute("DW_AT_type")
            if current.tag == "DW_TAG_const_type":
                return True
    return False


def _renamed(ctype, name):
    if ctype.kind in ("base", "enum", "pointer"):
        return CType(ctype.kind, name, ctype.size, ctype.encoding, ctype.members, ctype.element, ctype.dims,
                     ctype.enumerators)
    if ctype.kind in ("struct", "union") and ctype.name in ("struct", "union"):
        ctype.name = name  # 匿名结构体 typedef，原地改名（同一对象被缓存共享）
    return ctype


def _type_name(die):
    for _ in range(8):
        name = _text(die.attributes.get("DW_AT_name"))
        if name:
            return name
        if "DW_AT_type" not in die.attributes:
            return "void"
        die = die.get_DIE_from_attribute("DW_AT_type")
    return "?"
