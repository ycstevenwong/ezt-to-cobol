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


# COBOL reserved words the LLM most commonly tries to use as bare paragraph
# names.  Compile would fail on these — the post-processor below renames
# every definition + reference to <NAME>-RTN.
# NOTE: EXIT is intentionally NOT in this set — it's a valid statement and
# every  <PARA>-EXIT  paragraph body contains a bare  EXIT.  line.
_RESERVED_PARA_WORDS = {
    "INITIAL", "INITIALIZE", "TERMINATE",
    "START", "STOP", "END", "DATA", "SECTION", "DIVISION",
    "OPEN", "CLOSE", "READ", "WRITE", "REWRITE", "DELETE",
    "MOVE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE", "COMPUTE",
    "DISPLAY", "ACCEPT", "PERFORM", "GOBACK", "CALL",
    "IF", "ELSE", "EVALUATE", "WHEN",
    "SEARCH", "SORT", "MERGE", "STRING", "UNSTRING", "INSPECT",
    "SET", "REPLACE", "COPY",
}

# Paragraph definitions live in Area A (col 8 → up to ~10 leading spaces).
# Restricting to that range excludes Area-B lines like  '           EXIT.'
# which are statements, not paragraph headers.
_PARA_DEF_RE = re.compile(r"^ {0,10}([A-Z][A-Z0-9-]*)\.\s*$", re.MULTILINE)


# IS INTEGER is not COBOL syntax — the equivalent class test is IS NUMERIC.
# The LLM occasionally emits the wrong form despite the prompt; rewrite it.
_IS_INTEGER_RE = re.compile(r"\bIS\s+(NOT\s+)?INTEGER\b", re.IGNORECASE)


def _fix_integer_class_test(cobol: str) -> str:
    """Rewrite  IS [NOT] INTEGER  →  IS [NOT] NUMERIC  in procedure code."""
    return _IS_INTEGER_RE.sub(
        lambda m: f"IS {m.group(1) or ''}NUMERIC",
        cobol,
    )


def _ensure_period_before_paragraphs(cobol: str) -> str:
    """Insert a missing period at the end of the statement that precedes
    each Area-A paragraph header.

    COBOL requires every paragraph's last statement to be terminated by a
    period.  The LLM often forgets this, e.g.:
           MAIN-PROCESS.
               PERFORM OPEN-FILES THRU OPEN-FILES-EXIT
               STOP RUN              <-- missing '.'
           MAIN-PROCESS-EXIT.

    The compiler then chains  STOP RUN MAIN-PROCESS-EXIT.  into one
    invalid statement.  This walks the source: whenever a line matches
    a paragraph definition, look backward past blank and comment lines
    to find the last code line and ensure it ends with a period.
    """
    lines = cobol.splitlines()
    for i, line in enumerate(lines):
        if not _PARA_DEF_RE.match(line):
            continue
        # Look back for the last non-blank, non-comment line.
        j = i - 1
        while j >= 0:
            prev_stripped = lines[j].rstrip()
            if not prev_stripped:
                j -= 1
                continue
            # Comment lines have '*' at col 7 (1-indexed) → index 6.
            if len(lines[j]) > 6 and lines[j][6] == "*":
                j -= 1
                continue
            if not prev_stripped.endswith("."):
                lines[j] = prev_stripped + "."
            break
    return "\n".join(lines)


def _rename_reserved_paragraphs(cobol: str) -> str:
    """Rewrite any paragraph definition whose name is a COBOL reserved word.

    Scans for paragraph headers like  INITIAL.  /  INITIAL-EXIT.  whose base
    (the part before any -EXIT suffix) matches a reserved word, then rewrites
    every occurrence of that base — definitions and PERFORM references —
    to base+'-RTN'.  Paragraphs that aren't reserved are left alone.
    """
    rename: set = set()
    for m in _PARA_DEF_RE.finditer(cobol):
        name = m.group(1).upper()
        base = name[:-5] if name.endswith("-EXIT") else name
        if base in _RESERVED_PARA_WORDS:
            rename.add(base)
    if not rename:
        return cobol
    # Apply renames longest-first so prefixes don't shadow longer ones.
    for base in sorted(rename, key=len, reverse=True):
        cobol = re.sub(rf"\b{re.escape(base)}\b", f"{base}-RTN", cobol)
    return cobol


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
    (TITLE / HDG / DTL / LINE / FOOT) can be generated deterministically —
    gen_report_ws needs the field-PIC lookup from the preamble.  When
    `source` is omitted, the preamble is reconstructed from the FILE_DEF
    and FIELD_DEF sections (already present in `sections`), so direct
    callers that skip the source parameter still get the layouts.
    """
    if source:
        preamble = parse_preamble(source)
    else:
        # Stitch the preamble back together from the parsed sections.
        combined = "\n".join(
            s.content for s in sections
            if s.type in (SectionType.FILE_DEF, SectionType.FIELD_DEF)
        )
        preamble = parse_preamble(combined) if combined.strip() else None

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
            # Prefer the pre-generated layout that convert_all stashed under
            # 'report_ws:<name>' — that's the same text the LLM saw in its
            # context, so the procedure code references known identifiers.
            # Fall back to generating locally for direct callers that
            # bypass convert_all (tests, ad-hoc scripts).
            py_ws = converted.get(f"report_ws:{section.name}")
            if py_ws is None:
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
        # Rename any paragraph whose bare name collides with a COBOL
        # reserved word (INITIAL, TERMINATE, etc.) — both definitions
        # and PERFORM references are rewritten in lockstep.
        clean_proc = _rename_reserved_paragraphs(clean_proc)
        # COBOL has no IS INTEGER class test — rewrite to IS NUMERIC.
        clean_proc = _fix_integer_class_test(clean_proc)
        # Ensure each paragraph's last statement ends with a period so the
        # next paragraph header doesn't get parsed as part of it.
        clean_proc = _ensure_period_before_paragraphs(clean_proc)
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
# Any line longer than 72 chars must be continued.  Two continuation flavors:
#
#   • Statement continuation (between two complete tokens) — NO indicator;
#     content resumes in Area B (col 12).  In COBOL fixed format the line
#     break is treated as inter-token whitespace, so  OCCURS 12 \n TIMES
#     parses as  OCCURS 12 TIMES.
#
#   • Literal continuation (when the break falls inside a quoted literal)
#     — '-' in col 7 of the continuation; the line concatenates with NO
#     intervening space.  We close the quote on line 1 and re-open it on
#     the continuation, e.g.
#         'A LONG STRING THAT WOULD'
#        -    'OVERFLOW THE LINE'.
#
# Using '-' for ordinary token continuation produces invalid COBOL because
# the compiler concatenates the last word of line 1 with the first word
# of line 2 (e.g. OCCURS 12 + -TIMES → "12TIMES").

_MAX_COL = 72
_CONT_PREFIX = " " * 6 + "-" + " " * 4   # cols 1-6 blank, col 7 '-', cols 8-11 blank
_CONT_AREA_B = " " * 11                   # cols 1-11 blank, content from col 12


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
        # Statement continuation between complete tokens — NO '-' indicator.
        # The line break itself is treated as whitespace by the COBOL parser.
        first = line[:break_at]
        rest = line[break_at:].lstrip(" ")
        return [first] + _wrap_line(_CONT_AREA_B + rest)

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
