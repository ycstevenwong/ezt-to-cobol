"""Rule-based COBOL generator for FILE_DEF and FIELD_DEF sections."""
from dataclasses import dataclass
from typing import List, Union

from src.structured_parser import EZTDefine, EZTField, EZTFile, parse_preamble

# COBOL area indentation
_A = " " * 7   # Area A (col 8)  — FD, 01-level
_B = " " * 11  # Area B (col 12) — 05-level, FD clauses
_C = " " * 15  # col 16          — 10-level (inside REDEFINES)


# ── PIC generation ─────────────────────────────────────────────────────────────

def _pic(ftype: str, length: int, decimals: int) -> str:
    t = ftype.upper()
    if t == "N":
        return f"PIC 9({length})"
    if t == "A":
        return f"PIC X({length})"
    if t == "P":
        int_d = length - decimals
        return f"PIC S9({int_d})V9({decimals}) COMP-3" if decimals else f"PIC S9({length}) COMP-3"
    if t == "B":
        return f"PIC S9({length}) COMP"
    return f"PIC X({length})"


# ── REDEFINES detection ─────────────────────────────────────────────────────────

@dataclass
class _Simple:
    f: EZTField


@dataclass
class _Redefines:
    base: EZTField
    groups: List[List[EZTField]]  # each inner list is one REDEFINES alternative


def _detect_layout(fields: List[EZTField]) -> List[Union[_Simple, _Redefines]]:
    """Group fields into sequential items and REDEFINES groups using source order.

    Processes fields in the order they appear in the EZT source (not sorted by
    position).  A field whose start-col falls within the byte range of the last
    sequential field is treated as a REDEFINES of that field.  A new REDEFINES
    alternative starts whenever the start-col goes backward past the running
    end-position of the current alternative.
    """
    if not fields:
        return []

    result: List[Union[_Simple, _Redefines]] = []
    seq_end = 0          # byte-end of the last top-level sequential field
    current_base: Optional[EZTField] = None
    current_groups: List[List[EZTField]] = []
    current_group: List[EZTField] = []
    group_pos = 0        # running end within the current REDEFINES alternative

    def _flush() -> None:
        nonlocal current_base, current_groups, current_group, group_pos
        if current_group:
            current_groups.append(current_group)
        if current_base is not None:
            result.append(_Redefines(base=current_base, groups=current_groups))
        current_base, current_groups, current_group, group_pos = None, [], [], 0

    for f in fields:
        if f.start > seq_end:
            # Non-overlapping — sequential field
            _flush()
            result.append(_Simple(f))
            seq_end = f.end
        else:
            # Overlaps with seq_end → REDEFINES
            if current_base is None:
                # Promote the last sequential field to REDEFINES base
                if result and isinstance(result[-1], _Simple):
                    current_base = result.pop().f
                    group_pos = current_base.start
                else:
                    # No explicit base field — add field sequentially and move on
                    result.append(_Simple(f))
                    seq_end = max(seq_end, f.end)
                    continue

            if f.start < group_pos and current_group:
                # Start goes backward → new REDEFINES alternative
                current_groups.append(current_group)
                current_group = [f]
                group_pos = f.start + f.physical_bytes
            else:
                current_group.append(f)
                group_pos = max(group_pos, f.start + f.physical_bytes)

    _flush()
    return result


# ── Naming helpers ─────────────────────────────────────────────────────────────

def _prefix(file_name: str) -> str:
    """Short prefix for COBOL field names derived from the file name."""
    name = file_name.upper()
    if name.endswith("FILE"):
        name = name[:-4]
    return (name[:4] + "-") if name else "F-"


def _cname(file_name: str, field_name: str) -> str:
    return (_prefix(file_name) + field_name.upper())[:30]


# ── Record layout ───────────────────────────────────────────────────────────────

def _record_layout(file: EZTFile) -> List[str]:
    lines = [f"{_A}01  {file.name}-REC."]
    layout = _detect_layout(file.fields)

    for item in layout:
        if isinstance(item, _Simple):
            f = item.f
            cname = _cname(file.name, f.name)
            lines.append(f"{_B}05  {cname:<33} {_pic(f.type, f.length, f.decimals)}.")

        elif isinstance(item, _Redefines):
            base = item.base
            base_cname = _cname(file.name, base.name)
            lines.append(f"{_B}05  {base_cname:<33} {_pic(base.type, base.length, base.decimals)}.")
            for group in item.groups:
                if group:
                    grp_name = (_cname(file.name, group[0].name) + "-GRP")[:30]
                    lines.append(f"{_B}05  {grp_name:<33} REDEFINES {base_cname}.")
                    for f in group:
                        cname = _cname(file.name, f.name)
                        lines.append(f"{_C}10  {cname:<33} {_pic(f.type, f.length, f.decimals)}.")
    return lines


# ── FILE-CONTROL ────────────────────────────────────────────────────────────────
# No leading spaces here — assembler's _indent() adds 11 spaces when assembling.

_ORG = {"DISK": "SEQUENTIAL", "TAPE": "SEQUENTIAL", "VSAM": "INDEXED"}
_ACC = {"DISK": "SEQUENTIAL", "TAPE": "SEQUENTIAL", "VSAM": "RANDOM"}


def gen_file_control(files: List[EZTFile]) -> str:
    blocks = []
    for f in files:
        org = _ORG.get(f.org, "SEQUENTIAL")
        acc = _ACC.get(f.org, "SEQUENTIAL")
        blocks.append(
            f"SELECT {f.name}\n"
            f"    ASSIGN TO {f.name}\n"
            f"    ORGANIZATION IS {org}\n"
            f"    ACCESS MODE IS {acc}."
        )
    return "\n".join(blocks)


# ── FILE SECTION ────────────────────────────────────────────────────────────────
# Full COBOL indentation required — assembler inserts this verbatim.

def gen_file_section(files: List[EZTFile]) -> str:
    blocks = []
    for f in files:
        fd = [f"{_A}FD  {f.name}"]
        if f.rec_length:
            fd.append(f"{_B}RECORD CONTAINS {f.rec_length} CHARACTERS.")
        else:
            fd.append(f"{_B}RECORD CONTAINS 0 RECORDS.")
        fd += _record_layout(f)
        blocks.append("\n".join(fd))
    return "\n".join(blocks)


# ── WORKING-STORAGE ─────────────────────────────────────────────────────────────

def gen_working_storage(defines: List[EZTDefine]) -> str:
    lines = []
    for d in defines:
        pic_str = _pic(d.type, d.length, d.decimals)
        if d.value is not None:
            if d.type.upper() in ("N", "P", "B") and d.value in ("0", ""):
                val_clause = " VALUE ZERO"
            elif d.type.upper() in ("N", "P", "B"):
                val_clause = f" VALUE {d.value}"
            else:
                val_clause = f" VALUE '{d.value}'"
        else:
            val_clause = ""
        lines.append(f"{_A}01  {d.name:<33} {pic_str}{val_clause}.")
    return "\n".join(lines)


# ── Public API ──────────────────────────────────────────────────────────────────

def convert_file_def(source: str) -> str:
    """Generate FILE-CONTROL and FILE SECTION COBOL from the full EZT source.

    Needs the full source (not just the FILE_DEF section content) so it can
    associate field definition lines with their parent FILE statements.
    Returns text with --- FILE-CONTROL --- and --- FILE-SECTION --- markers.
    """
    preamble = parse_preamble(source)
    fc = gen_file_control(preamble.files)
    fs = gen_file_section(preamble.files)
    return f"--- FILE-CONTROL ---\n{fc}\n--- FILE-SECTION ---\n{fs}"


def convert_field_def(field_def_content: str) -> str:
    """Generate WORKING-STORAGE entries from DEFINE statements in FIELD_DEF content."""
    defines: List[EZTDefine] = []
    for line in field_def_content.splitlines():
        tokens = line.strip().split()
        if not tokens or tokens[0].upper() != "DEFINE":
            continue
        if len(tokens) < 4:
            continue
        name = tokens[1].upper()
        ftype = tokens[2].upper()
        try:
            length = int(tokens[3])
        except ValueError:
            continue
        decimals = 0
        value = None
        i = 4
        while i < len(tokens):
            if tokens[i].upper() == "VALUE" and i + 1 < len(tokens):
                value = tokens[i + 1].strip("'\"")
                i += 2
            elif tokens[i].isdigit():
                decimals = int(tokens[i])
                i += 1
            else:
                i += 1
        defines.append(EZTDefine(name=name, type=ftype, length=length,
                                  decimals=decimals, value=value))
    return gen_working_storage(defines)
