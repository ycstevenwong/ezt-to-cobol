"""Parse Easytrieve source into logical sections for section-by-section conversion."""
import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple


class SectionType(Enum):
    FILE_DEF = "file_definitions"
    FIELD_DEF = "field_definitions"
    JOB = "job"
    REPORT = "report"


@dataclass
class EZTSection:
    type: SectionType
    name: str
    content: str


# Keywords that always start a new top-level section.
# Encountering any of these while inside a block signals the block has ended,
# even if no explicit END-* keyword was present.
_SECTION_STARTERS = re.compile(
    r"^\s*(FILE|JOB|REPORT|PARM)\b", re.IGNORECASE
)


def _is_comment_or_blank(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("*") or stripped.startswith("//")


def parse_ezt(source: str) -> List[EZTSection]:
    """Split EZT source into ordered logical sections."""
    lines = source.splitlines()
    sections: List[EZTSection] = []
    file_lines: List[str] = []
    field_lines: List[str] = []

    i = 0
    while i < len(lines):
        line = lines[i]

        if _is_comment_or_blank(line):
            i += 1
            continue

        tokens = line.strip().split()
        first = tokens[0].upper()

        if first == "FILE":
            file_lines.append(line)
            i += 1

        elif first == "JOB":
            _flush_preamble(sections, file_lines, field_lines)
            file_lines, field_lines = [], []
            block, i = _collect_block(lines, i, end_pattern=r"^\s*(END-JOB|ENDJOB)\b")
            sections.append(EZTSection(SectionType.JOB, "JOB", "\n".join(block)))

        elif first == "REPORT":
            _flush_preamble(sections, file_lines, field_lines)
            file_lines, field_lines = [], []
            report_name = tokens[1] if len(tokens) > 1 else f"REPORT_{i}"
            block, i = _collect_block(lines, i, end_pattern=r"^\s*(END-REPORT|ENDREPORT)\b")
            sections.append(EZTSection(SectionType.REPORT, report_name, "\n".join(block)))

        else:
            field_lines.append(line)
            i += 1

    _flush_preamble(sections, file_lines, field_lines)
    return sections


def _collect_block(
    lines: List[str],
    start: int,
    end_pattern: Optional[str] = None,
) -> Tuple[List[str], int]:
    """Collect lines for a block beginning at `start`.

    Termination rules (in priority order):
    1. Explicit end keyword matching `end_pattern` — include the line, then stop.
    2. A new top-level section keyword (_SECTION_STARTERS) — stop WITHOUT
       consuming the line so the outer loop can handle it as the next section.
    3. End of file — stop.

    Blank and comment lines are always absorbed into the current block.
    """
    block = [lines[start]]
    i = start + 1

    while i < len(lines):
        line = lines[i]

        # Blank / comment lines always belong to the current block
        if _is_comment_or_blank(line):
            block.append(line)
            i += 1
            continue

        # Explicit end keyword — include and stop
        if end_pattern and re.match(end_pattern, line, re.IGNORECASE):
            block.append(line)
            i += 1
            break

        # New top-level section — stop WITHOUT consuming (outer loop takes over)
        if _SECTION_STARTERS.match(line):
            break

        block.append(line)
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
