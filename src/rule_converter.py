"""Rule-based COBOL generator for FILE_DEF and FIELD_DEF sections."""
from dataclasses import dataclass
from typing import List, Optional

from src.structured_parser import EZTDefine, EZTField, EZTFile, parse_preamble

# COBOL area indentation
_A = " " * 7   # Area A (col 8)  — FD, 01-level
_B = " " * 11  # Area B (col 12) — 05-level, FD clauses

_PIC_COL = 49  # target 0-indexed column for the PIC keyword (consistent across all depths)


def _field_line(prefix: str, name: str, pic: str) -> str:
    """Return a COBOL data-item line with PIC aligned to _PIC_COL."""
    name_width = max(_PIC_COL - len(prefix) - 1, len(name))
    return f"{prefix}{name:<{name_width}} {pic}."


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


# ── Containment tree ────────────────────────────────────────────────────────────

@dataclass
class _TreeNode:
    field: EZTField
    children: List['_TreeNode']


def _build_tree(fields: List[EZTField]) -> List[_TreeNode]:
    """Build a containment tree from EZT fields sorted by position.

    Field B becomes a child of A when A's byte range fully contains B's range.
    The most specific (smallest) enclosing field is used as the parent.
    """
    if not fields:
        return []
    sorted_fields = sorted(fields, key=lambda f: (f.start, -f.end))
    nodes = [_TreeNode(field=f, children=[]) for f in sorted_fields]
    roots: List[_TreeNode] = []
    stack: List[_TreeNode] = []  # ancestor chain, most specific on top

    for node in nodes:
        while stack and stack[-1].field.end < node.field.end:
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)

    return roots


def _render_subtree(nodes: List[_TreeNode], depth: int, cur: int, end: int) -> List[str]:
    """Render sibling nodes at the given depth, inserting FILLER for byte gaps.

    depth — 1-based level multiplier (depth 1 → level 05, depth 2 → level 10, …)
    cur   — first absolute byte position expected at this depth (1-based)
    end   — last byte of the enclosing parent (for trailing FILLER)

    At depth 1, a node with children gets a two-item REDEFINES pattern:
      05  NAME          PIC X(n).          ← raw field
      05  NAME-FIELDS   REDEFINES NAME.    ← structured breakdown
          10  ...
    Deeper nodes with children are rendered as plain group items (no extra REDEFINES).
    """
    indent = " " * (7 + depth * 4)   # 11 spaces at depth=1 (05-level)
    lvl = f"{depth * 5:02d}"
    prefix = f"{indent}{lvl}  "
    lines = []

    for node in nodes:
        gap = node.field.start - cur
        if gap > 0:
            lines.append(_field_line(prefix, "FILLER", f"PIC X({gap})"))
        f = node.field
        fname = f.name[:30]

        if node.children and depth == 1:
            # Two-item REDEFINES: raw field then structured REDEFINES
            lines.append(_field_line(prefix, fname, _pic(f.type, f.length, f.decimals)))
            redef_name = (f.name + "-FIELDS")[:30]
            lines.append(f"{prefix}{redef_name} REDEFINES {fname}.")
            lines.extend(_render_subtree(node.children, depth + 1, f.start, f.end))
        elif node.children:
            # Deeper group: plain group item, no additional REDEFINES
            lines.append(f"{prefix}{fname}.")
            lines.extend(_render_subtree(node.children, depth + 1, f.start, f.end))
        else:
            lines.append(_field_line(prefix, fname, _pic(f.type, f.length, f.decimals)))

        cur = f.end + 1

    trailing = end - cur + 1
    if trailing > 0:
        lines.append(_field_line(prefix, "FILLER", f"PIC X({trailing})"))
    return lines


# ── Record layout ───────────────────────────────────────────────────────────────

def _record_layout(file: EZTFile) -> List[str]:
    roots = _build_tree(file.fields)
    if not roots:
        return [f"{_A}01  {file.name}-REC."]

    if len(roots) == 1 and roots[0].children:
        # Single enclosing field with sub-fields → two-01 structure:
        # first 01 holds the raw field; second 01 REDEFINES it with the hierarchy.
        root = roots[0]
        root_name = root.field.name[:30]
        full_name = (root.field.name + "-FULL")[:30]
        redef_name = (file.name + "-FIELDS")[:30]
        pic = _pic(root.field.type, root.field.length, root.field.decimals)
        lines = [
            f"{_A}01  {root_name}.",
            _field_line(f"{_B}05  ", full_name, pic),
            f"{_A}01  {redef_name} REDEFINES {root_name}.",
        ]
        lines.extend(_render_subtree(root.children, 1, root.field.start, root.field.end))
        return lines

    # Multiple roots or a single leaf → standard single-01 sequential layout.
    rec_end = file.rec_length or (roots[-1].field.end if roots else 0)
    lines = [f"{_A}01  {file.name}-REC."]
    lines.extend(_render_subtree(roots, 1, 1, rec_end))
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
