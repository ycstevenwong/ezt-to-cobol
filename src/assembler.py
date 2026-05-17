"""Assemble per-section COBOL output into a complete, compilable program."""
import re
from typing import Dict, List, Tuple

from src.parser import EZTSection, SectionType

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


def _split_file_def(cobol: str) -> Tuple[str, str]:
    """Split Claude's FILE_DEF output at the --- FILE-CONTROL --- / --- FILE-SECTION --- markers."""
    fc_marker = re.compile(r"^---\s*FILE-CONTROL\s*---", re.IGNORECASE | re.MULTILINE)
    fs_marker = re.compile(r"^---\s*FILE-SECTION\s*---", re.IGNORECASE | re.MULTILINE)

    fc_match = fc_marker.search(cobol)
    fs_match = fs_marker.search(cobol)

    if fc_match and fs_match:
        fc_text = cobol[fc_match.end(): fs_match.start()].strip()   # no indent; assembler adds 11
        fs_text = cobol[fs_match.end():].strip('\n')                 # preserve COBOL indentation
        return fc_text, fs_text

    # Fallback: heuristic split — SELECT lines → FILE-CONTROL, FD lines → FILE SECTION
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
    return "\n".join(fc_lines).strip(), "\n".join(fs_lines).strip()


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
            fc, fs = _split_file_def(cobol)
            if fc:
                file_control_parts.append(fc)
            if fs:
                file_section_parts.append(fs)

        elif section.type == SectionType.FIELD_DEF:
            clean = _strip_division_header(
                cobol, r"^\s*WORKING-STORAGE SECTION\.\s*$"
            )
            ws_parts.append(clean)

        elif section.type == SectionType.JOB:
            # Remove PROCEDURE DIVISION header if Claude included it (we add it ourselves)
            clean = _strip_division_header(
                cobol, r"^\s*PROCEDURE DIVISION[\w\s]*\.\s*$"
            )
            procedure_parts.append(clean)

        elif section.type == SectionType.REPORT:
            ws_extra, proc = _split_report(cobol)
            if ws_extra:
                ws_parts.append(ws_extra)
            if proc:
                procedure_parts.append(proc)

    # Build each division
    ident = _IDENT_DIV.format(program_id=program_name[:8].upper())

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
    proc_body = "\n\n".join(procedure_parts) if procedure_parts else "       STOP RUN."
    procedure_div = "       PROCEDURE DIVISION.\n" + proc_body

    return "\n\n".join([ident, env_div, data_div, procedure_div]) + "\n"


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line.strip() else line for line in text.splitlines())
