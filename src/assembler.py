"""Assemble per-section COBOL output into a complete, compilable program."""
import re
from typing import Dict, List, Tuple

from src.parser import EZTSection, SectionType
from src.rule_converter import gen_report_ws

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


def _split_report(cobol: str) -> Tuple[str, str]:
    """Split REPORT output into optional WS additions and procedure code."""
    ws_marker = re.compile(r"^---\s*WORKING-STORAGE\s*---", re.IGNORECASE | re.MULTILINE)
    proc_marker = re.compile(r"^---\s*PROCEDURE\s*---", re.IGNORECASE | re.MULTILINE)

    ws_m = ws_marker.search(cobol)
    pr_m = proc_marker.search(cobol)

    if ws_m and pr_m:
        ws_text = cobol[ws_m.end(): pr_m.start()].strip()
        proc_text = cobol[pr_m.end():].strip()
        return ws_text, proc_text

    return "", cobol.strip()


def _strip_division_header(cobol: str, header_pattern: str) -> str:
    """Remove a division/section header line if the model included it."""
    return re.sub(header_pattern, "", cobol, flags=re.IGNORECASE | re.MULTILINE).strip('\n')


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
) -> str:
    """Build a complete COBOL program from converted section outputs."""

    file_control_parts: List[str] = []
    file_section_parts: List[str] = []
    ws_parts: List[str] = []
    procedure_parts: List[str] = []

    for section in sections:
        key = _section_key(section)
        cobol = converted.get(key, "").strip('\n')
        if not cobol:
            continue

        if section.type == SectionType.FILE_DEF:
            fc, fs, ws = _split_file_def(cobol)
            if fc:
                file_control_parts.append(fc)
            if fs:
                file_section_parts.append(fs)
            if ws:
                ws_parts.append(ws)

        elif section.type == SectionType.FIELD_DEF:
            clean = _strip_division_header(
                cobol, r"^\s*WORKING-STORAGE SECTION\.\s*$"
            )
            ws_parts.append(clean)

        elif section.type == SectionType.JOB:
            # Take only what comes after PROCEDURE DIVISION header, discarding
            # any DATA DIVISION content the LLM may have emitted before it.
            proc_m = re.search(
                r"^\s*PROCEDURE DIVISION[\w\s]*\.\s*$", cobol,
                re.IGNORECASE | re.MULTILINE,
            )
            clean_proc = cobol[proc_m.end():].strip("\n") if proc_m else cobol.strip("\n")
            if clean_proc:
                procedure_parts.append(clean_proc)

        elif section.type == SectionType.REPORT:
            # Python generates fixed WS (counters, accumulators) deterministically
            py_ws = gen_report_ws(section.name, section.content)
            if py_ws:
                ws_parts.append(py_ws)
            # LLM generates field-specific WS (TITLE layout, PRINT detail, etc.)
            ws_extra, proc = _split_report(cobol)
            if ws_extra:
                ws_parts.append(ws_extra)
            if proc:
                procedure_parts.append(proc)

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

_MAX_COL = 72
_CONT_PREFIX = " " * 6 + "-" + " " * 4   # cols 1-6 blank, col 7 '-', cols 8-11 blank


def _wrap_line(line: str) -> List[str]:
    """Wrap a single line to fit within _MAX_COL using COBOL continuation."""
    if len(line) <= _MAX_COL:
        return [line]
    # Prefer to break at a space so we don't split a token
    break_at = line.rfind(" ", 7, _MAX_COL)
    if break_at <= 6:           # no usable space — force hard break
        break_at = _MAX_COL
    first = line[:break_at]
    rest = line[break_at:].lstrip(" ")
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
