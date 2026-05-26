"""Assemble per-section COBOL output into a complete, compilable program."""
import re
from typing import Dict, List, Tuple

from src.parser import EZTSection, SectionType
from src.rule_converter import gen_report_ws
from src.structured_parser import parse_preamble

_IDENT_DIV = """\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. {program_id}.
      *----------------------------------------------------------------*
      * Converted from Easytrieve by ezt-to-cobol
      *----------------------------------------------------------------*"""

_ENV_HEADER = """\
       ENVIRONMENT DIVISION.
       CONFIGURATION SECTION.
       SOURCE-COMPUTER. IBM-MAINFRAME.
       OBJECT-COMPUTER. IBM-MAINFRAME.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL."""

_DATA_DIV = "       DATA DIVISION."
_FILE_SEC = "       FILE SECTION."
_WS_SEC = "       WORKING-STORAGE SECTION."


def _section_key(section: EZTSection) -> str:
    return f"{section.type.value}:{section.name}"


def _split_file_def(cobol: str) -> Tuple[str, str, str]:
    """Split FILE_DEF output at its three marker blocks.

    Returns (file_control_text, file_section_text, ws_status_text).
    """
    fc_marker = re.compile(r"^---\s*FILE-CONTROL\s*---",   re.IGNORECASE | re.MULTILINE)
    fs_marker = re.compile(r"^---\s*FILE-SECTION\s*---",   re.IGNORECASE | re.MULTILINE)
    ws_marker = re.compile(r"^---\s*WORKING-STORAGE\s*---", re.IGNORECASE | re.MULTILINE)

    fc_m = fc_marker.search(cobol)
    fs_m = fs_marker.search(cobol)
    ws_m = ws_marker.search(cobol)

    if fc_m and fs_m:
        fc_text = cobol[fc_m.end(): fs_m.start()].strip()
        if ws_m:
            fs_text = cobol[fs_m.end(): ws_m.start()].strip('\n')
            ws_text = cobol[ws_m.end():].strip('\n')
        else:
            fs_text = cobol[fs_m.end():].strip('\n')
            ws_text = ""
        return fc_text, fs_text, ws_text

    # Fallback: heuristic split (no WS in this case)
    fc_lines, fs_lines = [], []
    in_fd = False
    for line in cobol.splitlines():
        stripped = line.strip().upper()
        if re.match(r"^FD\b", stripped):
            in_fd = True
        elif re.match(r"^SELECT\b", stripped):
            in_fd = False
        if in_fd:
            fs_lines.append(line)
        else:
            fc_lines.append(line)
    return "\n".join(fc_lines).strip(), "\n".join(fs_lines).strip(), ""


def split_ws_proc(cobol: str) -> Tuple[str, str]:
    """Split LLM output into optional WS additions and procedure code.

    Expected format from JOB / REPORT prompts:
        --- WORKING-STORAGE ---
        [optional 01-level items]
        --- PROCEDURE ---
        [procedure code]

    If markers are missing, returns ("", cobol) — i.e. treats the whole
    response as procedure code.
    """
    ws_marker = re.compile(r"^\s*---\s*WORKING-STORAGE\s*---\s*$",
                           re.IGNORECASE | re.MULTILINE)
    proc_marker = re.compile(r"^\s*---\s*PROCEDURE\s*---\s*$",
                             re.IGNORECASE | re.MULTILINE)

    ws_m = ws_marker.search(cobol)
    pr_m = proc_marker.search(cobol)

    if ws_m and pr_m and ws_m.end() <= pr_m.start():
        # Use strip('\n') instead of strip() — preserve Area A indentation on
        # the first line of each block; we only want to drop surrounding blank lines.
        ws_text = cobol[ws_m.end(): pr_m.start()].strip('\n')
        proc_text = cobol[pr_m.end():].strip('\n')
        return ws_text, proc_text

    return "", cobol.strip('\n')


def _strip_division_header(cobol: str, header_pattern: str) -> str:
    """Remove a division/section header line if the model included it."""
    return re.sub(header_pattern, "", cobol, flags=re.IGNORECASE | re.MULTILINE).strip('\n')


_LEVEL_LINE_RE = re.compile(r"^\s*(\d{2})(\s+.*)?$")


def _normalize_ws_indent(ws_text: str) -> str:
    """Anchor every level-number line to its COBOL fixed-format column.

    01-level items must start at col 8 (Area A); sub-levels (05, 10, ...)
    must start at col 12 (Area B).  The LLM sometimes emits these at
    column 1 — this rewrites each level-number line to the right column
    while leaving comment lines and continuation lines untouched.
    """
    out = []
    for line in ws_text.splitlines():
        m = _LEVEL_LINE_RE.match(line)
        if m:
            level = m.group(1)
            rest = m.group(2) or ""
            indent = " " * 7 if level == "01" else " " * 11
            out.append(f"{indent}{level}{rest}")
        else:
            out.append(line)
    return "\n".join(out)


_DATA_ITEM_RE = re.compile(r"^\s*\d{2}\s+\w", re.MULTILINE)
_DATA_SECTION_RE = re.compile(
    r"^\s*(?:WORKING-STORAGE|FILE|LINKAGE|LOCAL-STORAGE)\s+SECTION\b",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_data_decls(cobol: str) -> str:
    """Remove data-item declarations and section headers from procedure code.

    The LLM occasionally reproduces WS declarations it saw in context.
    Level-number lines (01 NAME PIC ...) and section headers are never
    valid inside PROCEDURE DIVISION.
    """
    lines = []
    for line in cobol.splitlines():
        if _DATA_ITEM_RE.match(line) or _DATA_SECTION_RE.match(line):
            continue
        lines.append(line)
    return "\n".join(lines)


def assemble(
    sections: List[EZTSection],
    converted: Dict[str, str],
    program_name: str = "EZTPROG",
    source: str = "",
) -> str:
    """Build a complete COBOL program from converted section outputs.

    Pass the original EZT source via `source` so per-report WS layouts
    (TITLE / HDG / DTL / FOOT) can be generated deterministically — Python
    needs the field-PIC lookup from the preamble to size detail columns.
    """
    # Parse the preamble once so gen_report_ws can resolve PRINT field PICs.
    preamble = parse_preamble(source) if source else None

    file_control_parts: List[str] = []
    file_section_parts: List[str] = []
    ws_parts: List[str] = []
    procedure_parts: List[str] = []

    for section in sections:
        key = _section_key(section)
        cobol = (converted.get(key) or "").strip('\n')

        if section.type == SectionType.FILE_DEF and cobol:
            fc, fs, ws = _split_file_def(cobol)
            if fc:
                file_control_parts.append(fc)
            if fs:
                file_section_parts.append(fs)
            if ws:
                ws_parts.append(ws)

        elif section.type == SectionType.FIELD_DEF and cobol:
            clean = _strip_division_header(
                cobol, r"^\s*WORKING-STORAGE SECTION\.\s*$"
            )
            ws_parts.append(clean)

        elif section.type == SectionType.REPORT:
            # Python generates the per-report WS deterministically: counters,
            # accumulators, and (when a preamble is available) the
            # WS-{RPT}-TITLE / -HDG / -DTL / -FOOT line layouts as well.
            # The LLM-generated procedure code is in the COMBINED_LOGIC key.
            py_ws = gen_report_ws(section.name, section.content, preamble=preamble)
            if py_ws:
                ws_parts.append(py_ws)
        # JOB sections contribute nothing here — their procedure code
        # comes from the combined-logic LLM call below.

    # Single combined JOB+REPORT LLM result — extract WS additions and
    # the unified PROCEDURE DIVISION exactly once.
    combined = (converted.get("logic:combined") or "").strip("\n")
    if combined:
        llm_ws, proc = split_ws_proc(combined)
        if llm_ws:
            cleaned_ws = _strip_division_header(
                llm_ws, r"^\s*WORKING-STORAGE SECTION\.\s*$"
            )
            ws_parts.append(_normalize_ws_indent(cleaned_ws))
        proc_m = re.search(
            r"^\s*PROCEDURE DIVISION[\w\s]*\.\s*$", proc,
            re.IGNORECASE | re.MULTILINE,
        )
        clean_proc = proc[proc_m.end():].strip("\n") if proc_m else proc.strip("\n")
        clean_proc = _strip_data_decls(clean_proc)
        if clean_proc:
            procedure_parts.append(clean_proc)

    # Build each division
    # COBOL PROGRAM-ID: letters, digits, hyphens only — strip anything else
    # (e.g. a period that crept in from a multi-dot filename like TEST123.OLD)
    clean_id = re.sub(r"[^A-Z0-9-]", "", program_name.upper())[:8]
    ident = _IDENT_DIV.format(program_id=clean_id or "COBOLPGM")

    # ENVIRONMENT DIVISION
    if file_control_parts:
        fc_body = "\n".join(file_control_parts)
        env_div = _ENV_HEADER + "\n" + _indent(fc_body, 11)
    else:
        env_div = "       ENVIRONMENT DIVISION."

    # DATA DIVISION
    data_sections: List[str] = [_DATA_DIV]
    if file_section_parts:
        data_sections.append(_FILE_SEC)
        data_sections.append("\n".join(file_section_parts))
    data_sections.append(_WS_SEC)
    if ws_parts:
        data_sections.append("\n".join(ws_parts))
    else:
        data_sections.append("       01 FILLER PIC X.")
    data_div = "\n".join(data_sections)

    # PROCEDURE DIVISION
    proc_body = "\n\n".join(procedure_parts) if procedure_parts else "           STOP RUN."
    procedure_div = "       PROCEDURE DIVISION.\n" + proc_body

    cobol = "\n\n".join([ident, env_div, data_div, procedure_div]) + "\n"
    return _enforce_col_limit(cobol)


# ── Column-72 enforcement ────────────────────────────────────────────────────
#
# COBOL fixed format: columns 1-6 sequence, 7 indicator, 8-72 code, 73-80 id.
# Any line longer than 72 chars must be continued:
#   - current line ends at or before col 72
#   - next line has '-' in col 7, content resumes in Area B (col 12)
#
# Special handling when the break falls inside a quoted literal:
#   - close the quote on line 1
#   - continuation line has '-' in col 7 and re-opens with a fresh quote
#     in Area B (e.g.  'A LONG STRING THAT WOULD'
#                     -     'OVERFLOW THE LINE').

_MAX_COL = 72
_CONT_PREFIX = " " * 6 + "-" + " " * 4   # cols 1-6 blank, col 7 '-', cols 8-11 blank


def _find_literal_ranges(line: str) -> List[Tuple[int, int]]:
    """Return (open_pos, close_pos) of each quoted literal in the line.

    Tracks single and double quotes; doubled quotes ('' or "") inside a
    literal are treated as escaped quote characters, not as terminators.
    Positions are 0-based; if the literal is unclosed, close_pos is the
    final character index of the line.
    """
    ranges: List[Tuple[int, int]] = []
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c in ("'", '"'):
            quote = c
            start = i
            i += 1
            while i < n:
                if line[i] == quote:
                    if i + 1 < n and line[i + 1] == quote:
                        i += 2          # escaped quote inside the literal
                        continue
                    break
                i += 1
            ranges.append((start, i if i < n else n - 1))
            i += 1
        else:
            i += 1
    return ranges


def _wrap_line(line: str) -> List[str]:
    """Wrap a single line to fit within _MAX_COL using COBOL continuation."""
    if len(line) <= _MAX_COL:
        return [line]

    literal_ranges = _find_literal_ranges(line)

    def in_literal(pos: int) -> bool:
        return any(s < pos < e for s, e in literal_ranges)

    # Prefer a break at a space OUTSIDE any quoted literal so we don't
    # split a token or the contents of a literal.  Restrict the search
    # to positions in Area B (col 12+, index 11+) so we never break
    # inside a continuation prefix (which would not reduce line length
    # and would loop forever).
    break_at = -1
    for i in range(_MAX_COL - 1, 11, -1):
        if line[i] == " " and not in_literal(i):
            break_at = i
            break

    if break_at > 11:
        first = line[:break_at]
        rest = line[break_at:].lstrip(" ")
        return [first] + _wrap_line(_CONT_PREFIX + rest)

    # No safe break outside a literal — the literal itself must be split.
    # Close the quote on line 1, '-' continuation, fresh quote on line 2.
    # Closing the quote costs one char, so the latest position we can
    # close at is _MAX_COL - 1 (leaving 1 char for the quote = col 72).
    # Pick any literal that extends to at least col 72 — that includes
    # one whose closing quote is itself at col 72 with trailing tokens
    # (e.g. period) overflowing onto col 73+.
    containing = next(
        ((s, e) for s, e in literal_ranges if s < _MAX_COL and e >= _MAX_COL - 1),
        None,
    )
    if containing is not None:
        s, _e = containing
        quote = line[s]
        # Prefer to close at the last space inside the literal so we don't
        # split a word; fall back to hard-breaking at col 71 otherwise.
        # Lower bound must be > 11 so the close-and-reopen still shortens
        # the line (the continuation prefix itself is 11 chars).
        close_at = -1
        for i in range(_MAX_COL - 1, max(s, 11), -1):
            if line[i] == " ":
                close_at = i
                break
        if close_at > max(s, 11):
            first = line[:close_at] + quote          # close before the space
            rest  = quote + line[close_at + 1:]      # reopen, drop the space
        else:
            first = line[:_MAX_COL - 1] + quote      # hard close at col 71
            rest  = quote + line[_MAX_COL - 1:]
        return [first] + _wrap_line(_CONT_PREFIX + rest)

    # No literal containing col 72 and no usable space — force hard break.
    first = line[:_MAX_COL]
    rest = line[_MAX_COL:].lstrip(" ")
    return [first] + _wrap_line(_CONT_PREFIX + rest)


def _enforce_col_limit(cobol: str) -> str:
    """Wrap every line that exceeds column 72."""
    lines: List[str] = []
    for line in cobol.splitlines():
        lines.extend(_wrap_line(line))
    return "\n".join(lines) + "\n"


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line.strip() else line for line in text.splitlines())
