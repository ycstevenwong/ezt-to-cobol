"""Parse the EZT preamble into structured objects for rule-based COBOL generation."""
import math
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class EZTField:
    name: str
    start: int        # resolved byte position (1-based)
    length: int       # number of digits for P type, bytes for all others
    type: str         # N, A, P, B
    decimals: int = 0

    @property
    def physical_bytes(self) -> int:
        if self.type.upper() == "P":
            return math.ceil((self.length + 1) / 2)
        return self.length

    @property
    def end(self) -> int:
        return self.start + self.physical_bytes - 1


@dataclass
class EZTFile:
    name: str
    org: str          # DISK, TAPE, VSAM
    rec_length: int   # 0 = not specified
    fields: List[EZTField] = field(default_factory=list)


@dataclass
class EZTDefine:
    name: str
    type: str
    length: int
    decimals: int = 0
    value: Optional[str] = None


@dataclass
class Preamble:
    files: List[EZTFile] = field(default_factory=list)
    defines: List[EZTDefine] = field(default_factory=list)


_SECTION_BREAK = re.compile(r"^\s*(JOB|REPORT)\b", re.IGNORECASE)
_COMMENT = re.compile(r"^\s*(\*|//)")


def _blank_or_comment(line: str) -> bool:
    return not line.strip() or bool(_COMMENT.match(line))


def _parse_define(tokens: List[str]) -> Optional[EZTDefine]:
    """Parse a DEFINE statement: DEFINE name type length [decimals] [VALUE literal]."""
    if len(tokens) < 4 or tokens[0].upper() != "DEFINE":
        return None
    name = tokens[1].upper()
    ftype = tokens[2].upper()
    try:
        length = int(tokens[3])
    except ValueError:
        return None
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
    return EZTDefine(name=name, type=ftype, length=length, decimals=decimals, value=value)


def _parse_ws_field(tokens: List[str]) -> Optional[EZTDefine]:
    """Parse a standalone WS field: name W length type [decimals] [VALUE literal].

    W in position 1 is the working-storage marker, replacing the start column
    used by file fields.  Example: SALARY W 4 P 2
    """
    if len(tokens) < 4:
        return None
    if tokens[1].upper() != "W":
        return None
    try:
        length = int(tokens[2])
    except ValueError:
        return None
    ftype = tokens[3].upper()
    if ftype not in ("N", "A", "P", "B"):
        return None
    name = tokens[0].upper()
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
    return EZTDefine(name=name, type=ftype, length=length, decimals=decimals, value=value)


def _parse_field(tokens: List[str], prev_end: int) -> Optional[EZTField]:
    if len(tokens) < 4:
        return None
    name = tokens[0]
    ftype = tokens[3].upper()
    if ftype not in ("N", "A", "P", "B"):
        return None
    try:
        length = int(tokens[2])
    except ValueError:
        return None
    decimals = int(tokens[4]) if len(tokens) > 4 and tokens[4].isdigit() else 0
    if tokens[1] == "*":
        start = prev_end + 1
    else:
        try:
            start = int(tokens[1])
        except ValueError:
            return None
    return EZTField(name=name, start=start, length=length, type=ftype, decimals=decimals)


def scan_ws_fields(content: str) -> Tuple[List[EZTDefine], str]:
    """Scan arbitrary content for WS field declarations and strip them out.

    Recognises both DEFINE statements and W-marker fields so that all WS
    declarations inside JOB/REPORT blocks are handled by Python, not the LLM.
    Returns (defines, cleaned_content_with_ws_lines_removed).
    """
    defines: List[EZTDefine] = []
    clean_lines: List[str] = []
    for line in content.splitlines():
        tokens = line.strip().split()
        if not tokens:
            clean_lines.append(line)
            continue
        d = _parse_define(tokens)
        if d:
            defines.append(d)
            continue
        ws = _parse_ws_field(tokens)
        if ws:
            defines.append(ws)
            continue
        clean_lines.append(line)
    return defines, "\n".join(clean_lines)


def parse_preamble(source: str) -> Preamble:
    """Parse FILE definitions (with their record fields) and DEFINE/standalone WS variables.

    Associates field definition lines with whichever FILE statement preceded them.
    A DEFINE or a W-marker field (name W length type) breaks the file association;
    subsequent W fields are added to defines as working-storage entries.
    """
    result = Preamble()
    current_file: Optional[EZTFile] = None
    prev_end = 0

    for line in source.splitlines():
        if _blank_or_comment(line):
            continue
        if _SECTION_BREAK.match(line):
            break
        tokens = line.strip().split()
        if not tokens:
            continue
        first = tokens[0].upper()

        if first == "PARM":
            continue  # PARM lines are not part of the record/WS layout — skip

        if first == "FILE":
            if len(tokens) < 3:
                continue
            org = tokens[2].upper()
            rec_len = int(tokens[3]) if len(tokens) > 3 and tokens[3].isdigit() else 0
            current_file = EZTFile(name=tokens[1].upper(), org=org, rec_length=rec_len)
            result.files.append(current_file)
            prev_end = 0

        elif first == "DEFINE":
            current_file = None  # DEFINE ends the current file's field association
            d = _parse_define(tokens)
            if d:
                result.defines.append(d)

        elif len(tokens) > 1 and tokens[1].upper() == "W":
            current_file = None  # W field breaks file association
            ws = _parse_ws_field(tokens)
            if ws:
                result.defines.append(ws)

        elif current_file is not None:
            f = _parse_field(tokens, prev_end)
            if f:
                current_file.fields.append(f)
                prev_end = f.end

    return result
