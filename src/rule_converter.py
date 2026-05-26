"""Rule-based COBOL generator for FILE_DEF and FIELD_DEF sections."""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

from src.structured_parser import EZTDefine, EZTField, EZTFile, Preamble, parse_preamble

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
        if decimals:
            int_d = length - decimals
            return f"PIC 9({int_d})V9({decimals})"
        return f"PIC 9({length})"
    if t == "A":
        return f"PIC X({length})"
    if t == "P":
        # Packed decimal: N bytes hold N*2-1 digits (last byte = 1 digit + sign nibble).
        digits = length * 2 - 1
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
      10  FILLER REDEFINES CARD-START.
          15  CARD-START-6              PIC 9(6).
          15  FILLER                    PIC X(10).
      …

    Depth-2 decomposition group (e.g. CRANGE-KEY / CRANGE-ORG / CRANGE-TYPE):
      10  CRANGE-KEY                    PIC 9(6).
      10  FILLER REDEFINES CRANGE-KEY.
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
            # Each alternative: an unnamed (FILLER) REDEFINES group containing
            # the field + trailing FILLER. The wrapper group is never referenced
            # in PROCEDURE DIVISION logic — only the inner alternative is.
            child_indent = " " * (7 + (depth + 1) * 4)
            child_lvl = f"{(depth + 1) * 5:02d}"
            child_prefix = f"{child_indent}{child_lvl}  "
            for alt in same_start:
                af = alt.field
                alt_name = af.name[:30]
                lines.append(f"{prefix}FILLER REDEFINES {fname}.")
                lines.append(_field_line(child_prefix, alt_name,
                                         _pic(af.type, af.length, af.decimals)))
                filler_bytes = f.physical_bytes - af.physical_bytes
                if filler_bytes > 0:
                    lines.append(_field_line(child_prefix, "FILLER",
                                             f"PIC X({filler_bytes})"))
        elif node.children and depth <= 2:
            one_end = f.start + f.length - 1   # end of a single occurrence
            if f.occurs:
                # IBM COBOL forbids REDEFINES of an OCCURS item at the same level.
                # Render as a plain group with OCCURS so sub-fields nest inside it.
                lines.append(f"{prefix}{fname} OCCURS {f.occurs} TIMES.")
                lines.extend(_render_subtree(node.children, depth + 1, f.start, one_end))
            else:
                # Field with sub-fields: emit as elementary + FILLER REDEFINES group.
                # The wrapper group is never referenced — only its children are.
                lines.append(_field_line(prefix, fname, _pic(f.type, f.length, f.decimals)))
                lines.append(f"{prefix}FILLER REDEFINES {fname}.")
                lines.extend(_render_subtree(node.children, depth + 1, f.start, one_end))
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

_DEFAULT_PRINT_WIDTH = 133   # standard mainframe print-line width (132 + carriage-control)


def _effective_rec_length(file: EZTFile) -> int:
    """Pick a usable record length even when EZT omits it.

    Priority: explicit rec_length > end of the last declared field > 133.
    The 133 default covers the common case of a REPORT output file that
    EZT leaves unsized: every report needs *some* buffer to WRITE FROM.
    """
    if file.rec_length:
        return file.rec_length
    if file.fields:
        return max(f.end for f in file.fields)
    return _DEFAULT_PRINT_WIDTH


def _record_layout(file: EZTFile) -> List[str]:
    roots = _build_tree(file.fields)
    if not roots:
        # No declared fields — typical of an unfielded report output file.
        # Emit a single elementary record buffer at the effective length so
        # the FD is valid and the procedure code can WRITE / WRITE FROM it.
        rec_len = _effective_rec_length(file)
        rec_name = (file.name + "-REC")[:30]
        return [_field_line(f"{_A}01  ", rec_name, f"PIC X({rec_len})")]

    if len(roots) == 1 and roots[0].children and not roots[0].field.occurs:
        # Single enclosing field with sub-fields (no OCCURS) → single 01, two-05 structure:
        #   05 ROOT-FULL   PIC X(n).
        #   05 FILLER      REDEFINES ROOT-FULL.
        #      10 ...
        # The redefining group is unnamed (FILLER) — only the sub-fields under it
        # are referenced in PROCEDURE DIVISION logic.
        root = roots[0]
        root_name = root.field.name[:30]
        full_name = (root.field.name + "-FULL")[:30]
        pic = _pic(root.field.type, root.field.length, root.field.decimals)
        lines = [
            f"{_A}01  {root_name}.",
            _field_line(f"{_B}05  ", full_name, pic),
            f"{_B}05  FILLER REDEFINES {full_name}.",
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
        fd.append(f"{_B}RECORD CONTAINS {_effective_rec_length(f)} CHARACTERS.")
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
            #       05  FILLER        REDEFINES PARENT-FULL.
            #           10  sub-field ...
            # The redefining group is unnamed (FILLER) — only the sub-fields
            # under it are referenced in PROCEDURE DIVISION logic.
            full_name  = (d.name + "-FULL")[:30]
            lines.append(f"{_A}01  {d.name}.")
            lines.append(_field_line(f"{_B}05  ", full_name, pic_str + val_clause))
            lines.append(f"{_B}05  FILLER REDEFINES {full_name}.")
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

# ── REPORT directive parsing + per-report layouts ─────────────────────────────


@dataclass
class _ColFragment:
    """One COL-positioned text fragment on a TITLE/HEADING/LINE/FOOTING line."""
    col:  int   # 1-based column position
    text: str


@dataclass
class _ReportLine:
    """One text line from a numbered TITLE/HEADING/LINE/FOOTING directive.

    Either `text` is set (a single centered string) OR `fragments` is set
    (one or more COL-positioned pieces on the same line).  Never both.
    """
    line_num:  Optional[int] = None    # 1-based page line; None when omitted
    text:      str                      = ""
    fragments: List[_ColFragment]       = field(default_factory=list)


@dataclass
class _ReportDirectives:
    titles:        List[_ReportLine] = field(default_factory=list)
    headings:      List[_ReportLine] = field(default_factory=list)
    lines:         List[_ReportLine] = field(default_factory=list)   # extra LINE n 'text'
    footings:      List[_ReportLine] = field(default_factory=list)
    print_fields:  List[str]         = field(default_factory=list)
    control_field: Optional[str]     = None


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def _tokenize_directive(s: str) -> List[str]:
    """Split a directive payload into tokens, keeping quoted strings whole.

    Handles single and double quotes; doubled quotes inside a literal
    ('It''s') are treated as an escaped quote, not as a terminator.
    """
    tokens: List[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
            continue
        if c in ("'", '"'):
            q, j = c, i + 1
            while j < n:
                if s[j] == q:
                    if j + 1 < n and s[j + 1] == q:
                        j += 2
                        continue
                    break
                j += 1
            tokens.append(s[i:j + 1] if j < n else s[i:])
            i = j + 1
        else:
            j = i
            while j < n and not s[j].isspace():
                j += 1
            tokens.append(s[i:j])
            i = j
    return tokens


def _parse_text_line(rest: str) -> _ReportLine:
    """Parse a TITLE/HEADING/LINE/FOOTING payload.

    Supported forms (line number is optional throughout):
      NN 'text'                                   → centered text
      NN COL c 'text' [COL c 'text' ...]          → COL-positioned
      'text'                                       → centered, no line num
      COL c 'text' ...                             → positioned, no line num
    """
    toks = _tokenize_directive(rest)

    # Optional leading line number — only consume it if the next token
    # is NOT a quoted string starting with that digit's own content
    # (i.e. the digit really is a position marker, not the title text).
    line_num: Optional[int] = None
    if toks and toks[0].isdigit() and (len(toks) == 1 or not toks[1].isdigit()):
        line_num = int(toks[0])
        toks = toks[1:]

    # COL-positioned form: zero or more "COL n 'text'" triples.
    if toks and toks[0].upper() == "COL":
        frags: List[_ColFragment] = []
        i = 0
        while i + 2 < len(toks) + 1:
            if i + 2 >= len(toks) or toks[i].upper() != "COL":
                break
            try:
                col = int(toks[i + 1])
            except ValueError:
                break
            text_tok = toks[i + 2]
            if not (text_tok.startswith("'") or text_tok.startswith('"')):
                break
            frags.append(_ColFragment(col=col, text=_strip_quotes(text_tok)))
            i += 3
        return _ReportLine(line_num=line_num, fragments=frags)

    # Centered form: take the (single) quoted string literal.
    text = _strip_quotes(toks[0]) if toks else ""
    return _ReportLine(line_num=line_num, text=text)


def _parse_report_directives(content: str) -> _ReportDirectives:
    """Extract TITLE / HEADING / LINE / PRINT / FOOTING / CONTROL from a REPORT body."""
    d = _ReportDirectives()
    for raw in content.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("*"):
            continue
        parts = stripped.split(None, 1)
        kw = parts[0].upper() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        if kw == "TITLE":
            d.titles.append(_parse_text_line(rest))
        elif kw == "HEADING":
            d.headings.append(_parse_text_line(rest))
        elif kw == "LINE":
            d.lines.append(_parse_text_line(rest))
        elif kw == "FOOTING":
            d.footings.append(_parse_text_line(rest))
        elif kw == "PRINT":
            d.print_fields = rest.split()
        elif kw == "CONTROL":
            toks = rest.split()
            if toks:
                d.control_field = toks[0]
    return d


def _numbered_name(base: str, line_num: Optional[int]) -> str:
    """Append a -NN suffix when a line number is supplied; bare name otherwise."""
    return f"{base}-{line_num:02d}" if line_num is not None else base


def _display_width(ftype: str, length: int, decimals: int) -> int:
    """Approximate the print-column width for a field of the given EZT type."""
    t = ftype.upper()
    if t == "P":
        return max(length * 2 - 1, length)
    return length


def _build_field_lookup(preamble: Preamble) -> Dict[str, Union[EZTField, EZTDefine]]:
    """Map field/define NAMES (uppercased) → their parsed EZT object."""
    lookup: Dict[str, Union[EZTField, EZTDefine]] = {}
    for f in preamble.files:
        for fld in f.fields:
            lookup[fld.name.upper()] = fld
    for d in preamble.defines:
        lookup[d.name.upper()] = d
    return lookup


def _layout_block(name: str, items: List[Tuple[str, str, str]]) -> List[str]:
    """Emit ``01 <name>.`` followed by 05-level subfields.

    items: list of (sub_name | "FILLER", pic_string, value_clause_or_"")
    """
    out = [f"{_A}01  {name[:30]}."]
    for sub_name, pic, val in items:
        suffix = f"{pic} {val}".strip()
        out.append(_field_line(f"{_B}05  ", sub_name[:30], suffix))
    return out


def _gen_text_line_block(layout_name: str, text: str, width: int) -> List[str]:
    """Emit a centered single-text layout (TITLE / FOOTING)."""
    if not text:
        return []
    escaped = text.replace("'", "''")
    text_len = len(text)
    if text_len > width:
        escaped = escaped[:width]
        text_len = width
    lpad = (width - text_len) // 2
    rpad = width - text_len - lpad
    items: List[Tuple[str, str, str]] = []
    if lpad > 0:
        items.append(("FILLER", f"PIC X({lpad})", "VALUE SPACES"))
    items.append(("FILLER", f"PIC X({text_len})", f"VALUE '{escaped}'"))
    if rpad > 0:
        items.append(("FILLER", f"PIC X({rpad})", "VALUE SPACES"))
    return _layout_block(layout_name, items)


def _gen_positioned_line_block(layout_name: str,
                               fragments: List[_ColFragment],
                               width: int) -> List[str]:
    """Emit a layout with text fragments anchored to specific columns.

    Used for  TITLE NN COL c 'text' COL c 'text'  and the like.  Each
    fragment becomes a FILLER subfield with its literal VALUE, separated
    from neighbours by a FILLER gap sized to land on the requested column.
    Overlapping fragments are clipped to the column of the next one.
    """
    if not fragments:
        return []
    items: List[Tuple[str, str, str]] = []
    cur_col = 1
    for frag in sorted(fragments, key=lambda f: f.col):
        if frag.col > cur_col:
            gap = frag.col - cur_col
            items.append(("FILLER", f"PIC X({gap})", "VALUE SPACES"))
            cur_col += gap
        elif frag.col < cur_col:
            # Overlap with previous fragment — skip this one.
            continue
        text = frag.text
        text_len = len(text)
        if cur_col - 1 + text_len > width:
            text_len = width - (cur_col - 1)
            text = text[:text_len]
        if text_len <= 0:
            break
        escaped = text.replace("'", "''")
        items.append(("FILLER", f"PIC X({text_len})", f"VALUE '{escaped}'"))
        cur_col += text_len

    trailing = width - (cur_col - 1)
    if trailing > 0:
        items.append(("FILLER", f"PIC X({trailing})", "VALUE SPACES"))
    return _layout_block(layout_name, items)


def _gen_line_block(layout_name: str, line: _ReportLine, width: int) -> List[str]:
    """Dispatch to the centered or positioned emitter based on the line shape."""
    if line.fragments:
        return _gen_positioned_line_block(layout_name, line.fragments, width)
    return _gen_text_line_block(layout_name, line.text, width)


_LEFT_MARGIN = 1   # spaces before the first column on a detail/heading line
_COL_GAP     = 2   # spaces between adjacent columns


def _gen_dtl_block(rpt: str, fields: List[str],
                   lookup: Dict[str, Union[EZTField, EZTDefine]],
                   width: int) -> List[str]:
    if not fields:
        return []
    items: List[Tuple[str, str, str]] = []
    used = 0
    if _LEFT_MARGIN:
        items.append(("FILLER", f"PIC X({_LEFT_MARGIN})", "VALUE SPACES"))
        used += _LEFT_MARGIN
    for i, fname in enumerate(fields):
        if i > 0:
            items.append(("FILLER", f"PIC X({_COL_GAP})", "VALUE SPACES"))
            used += _COL_GAP
        fld = lookup.get(fname.upper())
        if fld is not None:
            col_w = _display_width(fld.type, fld.length, fld.decimals)
        else:
            col_w = 10
        items.append((f"WS-DTL-{fname}", f"PIC X({col_w})", ""))
        used += col_w
    trailing = width - used
    if trailing > 0:
        items.append(("FILLER", f"PIC X({trailing})", "VALUE SPACES"))
    return _layout_block(f"WS-{rpt}-DTL", items)


def _gen_hdg_block(rpt: str, fields: List[str],
                   lookup: Dict[str, Union[EZTField, EZTDefine]],
                   width: int) -> List[str]:
    """Column-heading layout — uses PRINT field names as the header text."""
    if not fields:
        return []
    items: List[Tuple[str, str, str]] = []
    used = 0
    if _LEFT_MARGIN:
        items.append(("FILLER", f"PIC X({_LEFT_MARGIN})", "VALUE SPACES"))
        used += _LEFT_MARGIN
    for i, fname in enumerate(fields):
        if i > 0:
            items.append(("FILLER", f"PIC X({_COL_GAP})", "VALUE SPACES"))
            used += _COL_GAP
        fld = lookup.get(fname.upper())
        col_w = _display_width(fld.type, fld.length, fld.decimals) if fld else 10
        header = fname[:col_w].ljust(col_w).replace("'", "''")
        items.append(("FILLER", f"PIC X({col_w})", f"VALUE '{header}'"))
        used += col_w
    trailing = width - used
    if trailing > 0:
        items.append(("FILLER", f"PIC X({trailing})", "VALUE SPACES"))
    return _layout_block(f"WS-{rpt}-HDG", items)


# ── REPORT WORKING-STORAGE generator ───────────────────────────────────────────


def gen_report_ws(report_name: str, content: str,
                  preamble: Optional[Preamble] = None) -> str:
    """Generate deterministic WORKING-STORAGE for a REPORT section.

    Always emits:
      - Page/line counters and limits (adjusted by LINESIZE/PAGESIZE directives)
      - PRINT-REC output buffer
      - SUM field accumulators (WS-{FIELD}-TOT / WS-{FIELD}-TOT-D)
      - COUNT accumulator (WS-{RPTNAME}-CNT / WS-{RPTNAME}-CNT-D)

    When `preamble` is supplied (so field PICs can be looked up), also emits:
      - WS-{RPTNAME}-TITLE   from TITLE 'text'
      - WS-{RPTNAME}-HDG     from PRINT field names (column headings)
      - WS-{RPTNAME}-DTL     from PRINT field list (one subfield per field)
      - WS-{RPTNAME}-FOOT    from FOOTING 'text'
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

    # Per-report layout items — generated only when we can resolve the
    # PRINT fields' PICs through the preamble lookup.  Skipping this when
    # no preamble is given preserves the original behavior for callers
    # that haven't been migrated yet.
    if preamble is not None:
        directives = _parse_report_directives(content)
        lookup = _build_field_lookup(preamble)
        for tl in directives.titles:
            lines.extend(_gen_line_block(
                _numbered_name(f"WS-{rpt}-TITLE", tl.line_num),
                tl, print_width
            ))
        for hl in directives.headings:
            lines.extend(_gen_line_block(
                _numbered_name(f"WS-{rpt}-HDG", hl.line_num),
                hl, print_width
            ))
        for ln in directives.lines:
            lines.extend(_gen_line_block(
                _numbered_name(f"WS-{rpt}-LINE", ln.line_num),
                ln, print_width
            ))
        if directives.print_fields:
            # Column-heading row auto-derived from PRINT field names.  Only
            # emitted when no explicit HEADING text was supplied — the
            # HEADING directives above are the user's own column row.
            if not directives.headings:
                lines.extend(_gen_hdg_block(
                    rpt, directives.print_fields, lookup, print_width
                ))
            lines.extend(_gen_dtl_block(
                rpt, directives.print_fields, lookup, print_width
            ))
        for fl in directives.footings:
            lines.extend(_gen_line_block(
                _numbered_name(f"WS-{rpt}-FOOT", fl.line_num),
                fl, print_width
            ))
        if directives.control_field:
            ctl = lookup.get(directives.control_field.upper())
            ctl_w = _display_width(ctl.type, ctl.length, ctl.decimals) if ctl else 10
            lines.append(_field_line(
                f"{_A}01  ",
                f"WS-{directives.control_field.upper()}-SAVE"[:30],
                f"PIC X({ctl_w}) VALUE SPACES",
            ))

    for raw in content.splitlines():
        tokens = raw.strip().split()
        if not tokens or tokens[0].upper() != "SUM":
            continue
        for fld_name in tokens[1:]:
            fname = fld_name.upper()
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


_VSAM_KEY_LEN = 5   # default synthetic-key length when EZT doesn't supply one


def _inject_vsam_key(file: EZTFile) -> EZTFile:
    """For a VSAM file, prepend a synthetic alphanumeric key field named
    <FILENAME>-KEY at position 1, shift every existing field down by the
    key length, and grow the record length to match.

    This makes the SELECT clause's  RECORD KEY IS <FILENAME>-KEY  resolve
    to a real field in the FD record layout.  If the EZT source already
    defines a field with that name (case-insensitive), the file passes
    through unchanged — the EZT-supplied key wins.
    """
    if file.org != "VSAM":
        return file
    key_name = (file.name + "-KEY")[:30]
    if any(f.name.upper() == key_name.upper() for f in file.fields):
        return file
    key_field = EZTField(name=key_name, start=1, length=_VSAM_KEY_LEN, type="A")
    shifted = [
        EZTField(
            name=f.name,
            start=f.start + _VSAM_KEY_LEN,
            length=f.length,
            type=f.type,
            decimals=f.decimals,
            occurs=f.occurs,
            heading=f.heading,
        )
        for f in file.fields
    ]
    return EZTFile(
        name=file.name,
        org=file.org,
        rec_length=file.rec_length + _VSAM_KEY_LEN if file.rec_length else 0,
        fields=[key_field] + shifted,
    )


def convert_file_def(source: str) -> str:
    """Generate FILE-CONTROL, FILE SECTION, and file-status WS from the full EZT source.

    Returns text with three marker-delimited blocks:
      --- FILE-CONTROL ---   SELECT … entries
      --- FILE-SECTION ---   FD … entries
      --- WORKING-STORAGE --- file-status 01-level fields
    """
    preamble = parse_preamble(source)
    files = [_inject_vsam_key(f) for f in preamble.files]
    fc = gen_file_control(files)
    fs = gen_file_section(files)
    ws = gen_file_status_ws(files)
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
