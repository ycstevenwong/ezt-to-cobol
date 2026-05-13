"""Parse Easytrieve source into logical sections for section-by-section conversion."""
import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Tuple


class SectionType(Enum):
    FILE_DEF = "file_definitions"
    FIELD_DEF = "field_definitions"
    MACRO = "macro"
    JOB = "job"
    REPORT = "report"


@dataclass
class EZTSection:
    type: SectionType
    name: str
    content: str


def parse_ezt(source: str) -> List[EZTSection]:
    """Split EZT source into ordered logical sections."""
    lines = source.splitlines()
    sections: List[EZTSection] = []
    file_lines: List[str] = []
    field_lines: List[str] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Skip comments and blank lines
        if not stripped or stripped.startswith("*") or stripped.startswith("//"):
            i += 1
            continue

        tokens = stripped.split()
        first = tokens[0].upper()

        if first == "FILE":
            file_lines.append(line)
            i += 1

        elif first == "MACRO":
            _flush_preamble(sections, file_lines, field_lines)
            file_lines, field_lines = [], []
            macro_name = tokens[1] if len(tokens) > 1 else f"MACRO_{i}"
            block, i = _collect_block(lines, i, r"^\s*(ENDMACRO|END-MACRO)\b")
            sections.append(EZTSection(SectionType.MACRO, macro_name, "\n".join(block)))

        elif first == "JOB":
            _flush_preamble(sections, file_lines, field_lines)
            file_lines, field_lines = [], []
            block, i = _collect_block(lines, i, r"^\s*(END-JOB|ENDJOB)\b")
            sections.append(EZTSection(SectionType.JOB, "JOB", "\n".join(block)))

        elif first == "REPORT":
            _flush_preamble(sections, file_lines, field_lines)
            file_lines, field_lines = [], []
            report_name = tokens[1] if len(tokens) > 1 else f"REPORT_{i}"
            block, i = _collect_block(lines, i, r"^\s*(END-REPORT|ENDREPORT)\b")
            sections.append(EZTSection(SectionType.REPORT, report_name, "\n".join(block)))

        else:
            # Treat as field/variable definition in the preamble
            field_lines.append(line)
            i += 1

    _flush_preamble(sections, file_lines, field_lines)
    return sections


def _collect_block(lines: List[str], start: int, end_pattern: str) -> Tuple[List[str], int]:
    """Collect lines from start until end_pattern, returning (block, next_index)."""
    block = [lines[start]]
    i = start + 1
    while i < len(lines):
        block.append(lines[i])
        if re.match(end_pattern, lines[i], re.IGNORECASE):
            i += 1
            break
        i += 1
    return block, i


def _flush_preamble(
    sections: List[EZTSection],
    file_lines: List[str],
    field_lines: List[str],
) -> None:
    if file_lines:
        sections.append(
            EZTSection(SectionType.FILE_DEF, "file_definitions", "\n".join(file_lines))
        )
    if field_lines:
        sections.append(
            EZTSection(SectionType.FIELD_DEF, "field_definitions", "\n".join(field_lines))
        )
