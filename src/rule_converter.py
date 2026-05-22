"""Rule-based COBOL generator for FILE_DEF and FIELD_DEF sections."""
from dataclasses import dataclass
from typing import List, Optional

from src.structured_parser import EZTDefine, EZTField, EZTFile, parse_preamble

# COBOL area indentation
_A = " " * 7   # Area A (col 8)  — FD, 01-level
_B = " " * 11  # Area B (col 12) — 05-level, FD clauses
_C = " " * 15  # col 16          — 10-level within a WS group

_PIC_COL = 49  # target 0-indexed column for the PIC keyword (consistent across all depths)


def _field_line(prefix: str, name: str, pic: str) -> str:
    """Return a COBOL data-item line with PIC aligned to _PIC_COL."""
    name_width = max(_PIC_COL - len(prefix) - 1, len(name))
    return f"{prefix}{name:<{name_width}} {pic}."


# ── PIC generation ─────────────────────────────────────────────────────────────

def _occurs(n: int) -> str:
    return f" OCCURS {n} TIMES" if n else ""


def _pic(ftype: str, length: int, decimals: int) -> str:
    t = ftype.upper()
    if t == "N":
        return f"PIC 9({length})"
    if t == "A":
        return f"PIC X({length})"
    if t == "P":
        # EZT length = physical packed bytes.  Digit count differs by parity:
        #   Odd  bytes N → (N-1)*2  digits  e.g. 5 bytes → (5-1)*2 = 8
        #   Even bytes N → N*2-1    digits  e.g. 4 bytes → 4*2-1   = 7
        # Both round-trip back correctly via ceil((digits+1)/2).
        digits = (length - 1) * 2 if length % 2 == 1 else length * 2 - 1
        int_d  = digits - decimals
        return f"PIC S9({int_d})V9({decimals}) COMP-3" if decimals else f"PIC S9({digits}) COMP-3"
    if t == "U":
        # Unsigned numeric display — like N but explicitly unsigned, no COMP-3.
        if decimals:
            int_d = length - decimals
            return f"PIC 9({int_d})V9({decimals})"
        return f"PIC 9({length})"
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


def _flatten_same_start_chain(node: _TreeNode) -> Optional[List[_TreeNode]]:
    """Return the list of REDEFINES alternatives when node's children form a
    same-start chain (each link has exactly one child that starts at the same
    absolute position as node).  Returns None otherwise.

    Example: CARD-START → CARD-START-10 → CARD-START-9 → CARD-START-6
    all start at position 17, so all three become direct REDEFINES of CARD-START.
    Returned list is sorted by field length ascending (shortest first).
    """
    if not node.children:
        return None
    chain: List[_TreeNode] = []
    cur = node
    while cur.children:
        if len(cur.children) != 1:
            return None  # multiple children → decomposition, not alternatives
        child = cur.children[0]
        if child.field.start != node.field.start:
            return None  # child at a different position → not a same-start chain
        chain.append(child)
        cur = child
    chain.sort(key=lambda n: n.field.end)
    return chain


def _render_subtree(nodes: List[_TreeNode], depth: int, cur: int, end: int) -> List[str]:
    """Render sibling nodes at the given depth, inserting FILLER for byte gaps.

    depth — 1-based level multiplier (depth 1 → level 05, depth 2 → level 10, …)
    cur   — first absolute byte position expected at this depth (1-based)
    end   — last byte of the enclosing parent (for trailing FILLER)

    Same-start chain (e.g. CARD-START / CARD-START-6 / CARD-START-9):
      10  CARD-START                    PIC 9(16).
      10  CARD-START-6 REDEFINES CARD-START  PIC 9(6).
      …

    Depth-2 decomposition group (e.g. CRANGE-KEY / CRANGE-ORG / CRANGE-TYPE):
      10  CRANGE-KEY                    PIC 9(6).
      10  CRANGE-KEY-FIELDS REDEFINES CRANGE-KEY.
          15  CRANGE-ORG                PIC 9(3).
          15  CRANGE-TYPE               PIC 9(3).
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

        if f.heading:
            lines.append(f"      * HEADING: {f.heading}")

        same_start = _flatten_same_start_chain(node)
        if same_start is not None:
            # Base field
            lines.append(_field_line(prefix, fname, _pic(f.type, f.length, f.decimals)))
            # Each alternative: a named REDEFINES group containing the field + FILLER
            child_indent = " " * (7 + (depth + 1) * 4)
            child_lvl = f"{(depth + 1) * 5:02d}"
            child_prefix = f"{child_indent}{child_lvl}  "
            for alt in same_start:
                af = alt.field
                alt_name = af.name[:30]
                # Group name: {base}-FIELDS-{suffix}, where suffix is the part of
                # the alternative name after the base name (e.g. "6" from CARD-START-6)
                if af.name.upper().startswith(f.name.upper() + "-"):
                    suffix = af.name[len(f.name) + 1:]
                else:
                    suffix = af.name
                grp_name = (f.name + "-FIELDS-" + suffix)[:30]
                lines.append(f"{prefix}{grp_name} REDEFINES {fname}.")
                lines.append(_field_line(child_prefix, alt_name,
                                         _pic(af.type, af.length, af.decimals)))
                filler_bytes = f.physical_bytes - af.physical_bytes
                if filler_bytes > 0:
                    lines.append(_field_line(child_prefix, "FILLER",
                                             f"PIC X({filler_bytes})"))
        elif node.children and depth <= 2:
            # Field with sub-fields: emit as elementary + named REDEFINES group.
            # Using a plain group (same name) would duplicate the field name at
            # an adjacent level, which causes COBOL compile errors.
            lines.append(_field_line(prefix, fname, _pic(f.type, f.length, f.decimals)))
            redef_name = (f.name + "-FIELDS")[:30]
            lines.append(f"{prefix}{redef_name} REDEFINES {fname}.")
            lines.extend(_render_subtree(node.children, depth + 1, f.start, f.end))
        elif node.children:
            # Plain group (depth > 2, level 15+)
            lines.append(f"{prefix}{fname}.")
            lines.extend(_render_subtree(node.children, depth + 1, f.start, f.end))
        else:
            lines.append(_field_line(prefix, fname,
                                     _pic(f.type, f.length, f.decimals) + _occurs(f.occurs)))

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
        # Single enclosing field with sub-fields → single 01, two-05 structure:
        #   05 ROOT-FULL   PIC X(n).
        #   05 FILE-FIELDS REDEFINES ROOT-FULL.
        #      10 ...
        root = roots[0]
        root_name = root.field.name[:30]
        full_name = (root.field.name + "-FULL")[:30]
        redef_name = (file.name + "-FIELDS")[:30]
        pic = _pic(root.field.type, root.field.length, root.field.decimals)
        lines = [
            f"{_A}01  {root_name}.",
            _field_line(f"{_B}05  ", full_name, pic),
            f"{_B}05  {redef_name} REDEFINES {full_name}.",
        ]
        lines.extend(_render_subtree(root.children, 2, root.field.start, root.field.end))
        return lines

    # Single leaf with no sub-fields → make the field the 01-level item directly.
    # Wrapping it in a group named {file}-REC would duplicate the field's own name.
    if len(roots) == 1 and not roots[0].children:
        root = roots[0]
        pic = _pic(root.field.type, root.field.length, root.field.decimals) + _occurs(root.field.occurs)
        lines = []
        if root.field.heading:
            lines.append(f"      * HEADING: {root.field.heading}")
        lines.append(_field_line(f"{_A}01  ", root.field.name[:30], pic))
        return lines

    # Multiple roots → sequential layout under a group record.
    # If the auto-generated group name clashes with a field name, rename it.
    rec_end = file.rec_length or (roots[-1].field.end if roots else 0)
    field_names = {f.name.upper() for f in file.fields}
    rec_name = f"{file.name}-REC"
    if rec_name.upper() in field_names:
        rec_name = f"{file.name}-RECORD"
    lines = [f"{_A}01  {rec_name[:30]}."]
    lines.extend(_render_subtree(roots, 1, 1, rec_end))
    return lines


# ── FILE-CONTROL ────────────────────────────────────────────────────────────────
# No leading spaces here — assembler's _indent() adds 11 spaces when assembling.

_ORG = {"DISK": "SEQUENTIAL", "TAPE": "SEQUENTIAL", "VSAM": "INDEXED",
        "PRINTER": "LINE SEQUENTIAL", "WORK": "SEQUENTIAL"}
_ACC = {"DISK": "SEQUENTIAL", "TAPE": "SEQUENTIAL", "VSAM": "RANDOM",
        "PRINTER": "SEQUENTIAL",     "WORK": "SEQUENTIAL"}


def gen_file_control(files: List[EZTFile]) -> str:
    blocks = []
    for f in files:
        org = _ORG.get(f.org, "SEQUENTIAL")
        acc = _ACC.get(f.org, "SEQUENTIAL")
        clauses = [
            f"SELECT {f.name}",
            f"    ASSIGN TO {f.name}",
            f"    ORGANIZATION IS {org}",
            f"    ACCESS MODE IS {acc}",
        ]
        if f.org == "VSAM":
            clauses.append(f"    RECORD KEY IS {(f.name + '-KEY')[:30]}")
        clauses.append(f"    FILE STATUS IS WS-{f.name}-STATUS.")
        blocks.append("\n".join(clauses))
    return "\n".join(blocks)


def gen_file_status_ws(files: List[EZTFile]) -> str:
    """Generate WORKING-STORAGE file-status fields (one PIC X(2) per file)."""
    lines = []
    for f in files:
        ws_name = f"WS-{f.name}-STATUS"
        lines.append(f"{_A}01  {ws_name:<33} PIC X(2) VALUE SPACES.")
    return "\n".join(lines)


# ── FILE SECTION ────────────────────────────────────────────────────────────────
# Full COBOL indentation required — assembler inserts this verbatim.

def gen_file_section(files: List[EZTFile]) -> str:
    blocks = []
    for f in files:
        fd = [f"{_A}FD  {f.name}"]
        if f.rec_length:
            fd.append(f"{_B}RECORD CONTAINS {f.rec_length} CHARACTERS.")
        else:
            fd.append(f"{_B}RECORD CONTAINS 0 CHARACTERS.")
        fd += _record_layout(f)
        blocks.append("\n".join(fd))
    return "\n".join(blocks)


# ── WORKING-STORAGE ─────────────────────────────────────────────────────────────

def gen_working_storage(defines: List[EZTDefine]) -> str:
    lines = []
    for d in defines:
        pic_str = _pic(d.type, d.length, d.decimals)
        if d.value is not None:
            if d.type.upper() in ("N", "P", "B", "U") and d.value in ("0", ""):
                val_clause = " VALUE ZERO"
            elif d.type.upper() in ("N", "P", "B", "U"):
                val_clause = f" VALUE {d.value}"
            else:
                val_clause = f" VALUE '{d.value}'"
        else:
            val_clause = ""
        if d.subfields:
            # 01-level REDEFINES is invalid in WORKING-STORAGE.
            # Wrap in a group item so REDEFINES sits at level 05:
            #   01  PARENT.
            #       05  PARENT-FULL   PIC X(n).
            #       05  PARENT-FIELDS REDEFINES PARENT-FULL.
            #           10  sub-field ...
            full_name  = (d.name + "-FULL")[:30]
            redef_name = (d.name + "-FIELDS")[:30]
            lines.append(f"{_A}01  {d.name}.")
            lines.append(_field_line(f"{_B}05  ", full_name, pic_str + val_clause))
            lines.append(f"{_B}05  {redef_name} REDEFINES {full_name}.")
            cur = 1
            for sf in sorted(d.subfields, key=lambda s: s.start):
                gap = sf.start - cur
                if gap > 0:
                    lines.append(_field_line(f"{_C}10  ", "FILLER", f"PIC X({gap})"))
                sf_pic = _pic(sf.type, sf.length, sf.decimals) + _occurs(sf.occurs)
                if sf.value is not None:
                    if sf.type.upper() in ("N", "P", "B", "U") and sf.value in ("0", ""):
                        sf_pic += " VALUE ZERO"
                    elif sf.type.upper() in ("N", "P", "B", "U"):
                        sf_pic += f" VALUE {sf.value}"
                    else:
                        sf_pic += f" VALUE '{sf.value}'"
                lines.append(_field_line(f"{_C}10  ", sf.name[:30], sf_pic))
                cur = sf.end + 1
            trailing = d.physical_bytes - cur + 1
            if trailing > 0:
                lines.append(_field_line(f"{_C}10  ", "FILLER", f"PIC X({trailing})"))
        else:
            full_line = f"{_A}01  {d.name:<33} {pic_str}{val_clause}{_occurs(d.occurs)}."
            if len(full_line) <= 72 or not val_clause:
                lines.append(full_line)
            else:
                # VALUE clause pushes the line past IBM COBOL's 72-column fixed-format
                # limit.  Put the PIC clause on line 1 (no period) and the VALUE
                # clause on a continuation line indented to Area B.
                lines.append(f"{_A}01  {d.name:<33} {pic_str}{_occurs(d.occurs)}")
                lines.append(f"{_B}{val_clause.lstrip()}.")

    return "\n".join(lines)


# ── Public API ──────────────────────────────────────────────────────────────────

def gen_report_ws(report_name: str, content: str) -> str:
    """Generate deterministic WORKING-STORAGE for a REPORT section.

    Covers items whose structure is fixed regardless of report content:
      - Page/line counters and limits (adjusted by LINESIZE/PAGESIZE directives)
      - PRINT-REC output buffer
      - SUM field accumulators (WS-{FIELD}-TOT / WS-{FIELD}-TOT-D)
      - COUNT accumulator (WS-{RPTNAME}-CNT / WS-{RPTNAME}-CNT-D)

    Field-specific layout items (TITLE, HEADING, PRINT detail line, FOOTING,
    CONTROL save area) are left to the LLM because they require column-position
    and field-PIC knowledge.
    """
    rpt = report_name.upper()
    line_limit  = 55
    page_limit  = 60
    print_width = 133

    for raw in content.splitlines():
        tokens = raw.strip().split()
        if not tokens:
            continue
        kw = tokens[0].upper()
        if kw == "LINESIZE" and len(tokens) > 1 and tokens[1].isdigit():
            print_width = int(tokens[1])
            line_limit  = print_width - 5
        elif kw == "PAGESIZE" and len(tokens) > 1 and tokens[1].isdigit():
            page_limit = int(tokens[1])
            line_limit = page_limit - 5

    lines: List[str] = [
        _field_line(f"{_A}01  ", "WS-PAGE-CTR",   f"PIC 9(4) VALUE ZERO"),
        _field_line(f"{_A}01  ", "WS-LINE-CTR",   f"PIC 9(3) VALUE ZERO"),
        _field_line(f"{_A}01  ", "WS-PAGE-LIMIT", f"PIC 9(3) VALUE {page_limit}"),
        _field_line(f"{_A}01  ", "WS-LINE-LIMIT", f"PIC 9(3) VALUE {line_limit}"),
        _field_line(f"{_A}01  ", "PRINT-REC",     f"PIC X({print_width}) VALUE SPACES"),
    ]

    for raw in content.splitlines():
        tokens = raw.strip().split()
        if not tokens or tokens[0].upper() != "SUM":
            continue
        for field in tokens[1:]:
            fname = field.upper()
            lines.append(_field_line(f"{_A}01  ", f"WS-{fname}-TOT"[:30],
                                     "PIC S9(12)V9(2) COMP-3 VALUE ZERO"))
            lines.append(_field_line(f"{_A}01  ", f"WS-{fname}-TOT-D"[:30],
                                     "PIC Z(11)9.99"))

    if any(raw.strip().upper().startswith("COUNT")
           for raw in content.splitlines()):
        lines.append(_field_line(f"{_A}01  ", f"WS-{rpt}-CNT"[:30],
                                 "PIC S9(8) COMP-3 VALUE ZERO"))
        lines.append(_field_line(f"{_A}01  ", f"WS-{rpt}-CNT-D"[:30],
                                 "PIC Z(7)9"))

    return "\n".join(lines)


def convert_file_def(source: str) -> str:
    """Generate FILE-CONTROL, FILE SECTION, and file-status WS from the full EZT source.

    Returns text with three marker-delimited blocks:
      --- FILE-CONTROL ---   SELECT … entries
      --- FILE-SECTION ---   FD … entries
      --- WORKING-STORAGE --- file-status 01-level fields
    """
    preamble = parse_preamble(source)
    fc = gen_file_control(preamble.files)
    fs = gen_file_section(preamble.files)
    ws = gen_file_status_ws(preamble.files)
    return (f"--- FILE-CONTROL ---\n{fc}\n"
            f"--- FILE-SECTION ---\n{fs}\n"
            f"--- WORKING-STORAGE ---\n{ws}")


def convert_field_def(field_def_content: str) -> str:
    """Generate WORKING-STORAGE entries from FIELD_DEF content.

    Handles both DEFINE statements and standalone WS fields (name type length).
    Content has already been processed by parse_ezt (join_continuations applied).
    """
    preamble = parse_preamble(field_def_content, already_joined=True)
    return gen_working_storage(preamble.defines)
