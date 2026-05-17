"""Parse the EZT preamble into structured objects for rule-based COBOL generation."""
import math
import re
from dataclasses import dataclass, field
from typing import List, Optional


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


_SECTION_BREAK = re.compile(r"^\s*(JOB|REPORT|PARM)\b", re.IGNORECASE)
_COMMENT = re.compile(r"^\s*(\*|//)")


def _blank_or_comment(line: str) -> bool:
    return not line.strip() or bool(_COMMENT.match(line))


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


def parse_preamble(source: str) -> Preamble:
    """Parse FILE definitions (with their record fields) and DEFINE variables.

    Associates field definition lines with whichever FILE statement preceded them.
    A DEFINE statement breaks the file association — subsequent fields that are not
    DEFINE statements are treated as orphaned and ignored.
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
            result.defines.append(
                EZTDefine(name=name, type=ftype, length=length, decimals=decimals, value=value)
            )

        elif current_file is not None:
            f = _parse_field(tokens, prev_end)
            if f:
                current_file.fields.append(f)
                prev_end = f.end

    return result
