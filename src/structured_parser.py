"""Parse the EZT preamble into structured objects for rule-based COBOL generation."""
import math
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class EZTField:
    name: str
    start: int        # resolved byte position (1-based)
    length: int       # physical byte length for ALL types (P is already packed)
    type: str         # N, A, P, B
    decimals: int = 0
    occurs: int = 0   # 0 = no OCCURS clause
    heading: Optional[str] = None

    @property
    def physical_bytes(self) -> int:
        return self.length  # length is always physical bytes in EZT

    @property
    def end(self) -> int:
        total = self.physical_bytes * (self.occurs or 1)
        return self.start + total - 1


@dataclass
class EZTFile:
    name: str
    org: str          # DISK, TAPE, VSAM
    rec_length: int   # 0 = not specified
    fields: List[EZTField] = field(default_factory=list)


@dataclass
class EZTWSSubfield:
    name: str
    start: int    # 1-based byte position within the parent field
    length: int
    type: str     # N, A, P, B
    decimals: int = 0
    occurs: int = 0

    @property
    def physical_bytes(self) -> int:
        return self.length  # length is always physical bytes in EZT

    @property
    def end(self) -> int:
        total = self.physical_bytes * (self.occurs or 1)
        return self.start + total - 1


@dataclass
class EZTDefine:
    name: str
    type: str
    length: int       # physical byte length for ALL types
    decimals: int = 0
    value: Optional[str] = None
    subfields: List[EZTWSSubfield] = field(default_factory=list)
    occurs: int = 0

    @property
    def physical_bytes(self) -> int:
        return self.length  # length is always physical bytes in EZT


@dataclass
class Preamble:
    files: List[EZTFile] = field(default_factory=list)
    defines: List[EZTDefine] = field(default_factory=list)


_SECTION_BREAK = re.compile(r"^\s*(JOB|REPORT)\b", re.IGNORECASE)
_COMMENT = re.compile(r"^\s*(\*|//)")


def _blank_or_comment(line: str) -> bool:
    return not line.strip() or bool(_COMMENT.match(line))


# Short-form EZT file organisation keywords → canonical name
_ORG_ALIASES: dict = {
    "VS":      "VSAM",
    "DA":      "DISK",
    "TA":      "TAPE",
    "PRINT":   "PRINTER",
    "PR":      "PRINTER",
    "WORK":    "WORK",
    "WK":      "WORK",
}


def _normalise_org(org: str) -> str:
    return _ORG_ALIASES.get(org.upper(), org.upper())


def _parse_heading_value(tokens: List[str], i: int) -> Tuple[str, int]:
    """Parse the text after the HEADING keyword.

    Handles both parenthesised form  HEADING ('ACCT NUM')
    and bare quoted form             HEADING 'TC'
    Returns (heading_text, next_token_index).
    """
    if i >= len(tokens):
        return "", i
    tok = tokens[i]
    if tok.startswith("("):
        # Collect tokens until the one containing the closing )
        parts: List[str] = []
        while i < len(tokens):
            parts.append(tokens[i])
            if ")" in tokens[i]:
                i += 1
                break
            i += 1
        raw = " ".join(parts).lstrip("(").rstrip(")")
    else:
        raw = tok
        i += 1
    return raw.strip().strip("'\"").strip(), i


def _parse_optional_attrs(tokens: List[str], start_idx: int):
    """Scan tokens from start_idx for optional decimals, VALUE, OCCURS, and HEADING.

    Returns (decimals, value, occurs, heading).
    """
    decimals = 0
    value = None
    occurs = 0
    heading = None
    i = start_idx
    while i < len(tokens):
        t = tokens[i].upper()
        if t == "VALUE" and i + 1 < len(tokens):
            value = tokens[i + 1].strip("'\"")
            i += 2
        elif t == "OCCURS" and i + 1 < len(tokens):
            try:
                occurs = int(tokens[i + 1])
            except ValueError:
                pass
            i += 2
        elif t == "HEADING":
            i += 1
            heading, i = _parse_heading_value(tokens, i)
        elif tokens[i].isdigit():
            decimals = int(tokens[i])
            i += 1
        else:
            i += 1
    return decimals, value, occurs, heading


def _parse_define(tokens: List[str]) -> Optional[EZTDefine]:
    """Parse a DEFINE statement: DEFINE name type length [decimals] [VALUE v] [OCCURS n]."""
    if len(tokens) < 4 or tokens[0].upper() != "DEFINE":
        return None
    name = tokens[1].upper()
    ftype = tokens[2].upper()
    try:
        length = int(tokens[3])
    except ValueError:
        return None
    decimals, value, occurs, _ = _parse_optional_attrs(tokens, 4)
    return EZTDefine(name=name, type=ftype, length=length,
                     decimals=decimals, value=value, occurs=occurs)


def _parse_ws_field(tokens: List[str]) -> Optional[EZTDefine]:
    """Parse a standalone WS field: name W length type [decimals] [VALUE v] [OCCURS n].

    W in position 1 is the working-storage marker, replacing the start column
    used by file fields.  Example: SALARY W 4 P 2
    """
    if len(tokens) < 4 or tokens[1].upper() != "W":
        return None
    try:
        length = int(tokens[2])
    except ValueError:
        return None
    ftype = tokens[3].upper()
    if ftype not in ("N", "A", "P", "B"):
        return None
    name = tokens[0].upper()
    decimals, value, occurs, _ = _parse_optional_attrs(tokens, 4)
    return EZTDefine(name=name, type=ftype, length=length,
                     decimals=decimals, value=value, occurs=occurs)


def _parse_ws_subfield(tokens: List[str], prev_end: int) -> Optional[EZTWSSubfield]:
    """Parse a WS sub-field referencing a parent WS field.

    Two forms (tokens[1] is the parent name, verified by caller):
      name parent  length  type  [decimals]   — no offset; starts right after prev_end
      name parent +N       length type [dec]  — +N is 0-based offset from parent start
                                                so start = N + 1 (1-based)
    Example:
      WS-HH  WS-SYSTIME  2 N      → start=1, length=2
      FILE1  WS-SYSTIME +2 1 A    → start=3, length=1
    """
    if len(tokens) < 4:
        return None
    name = tokens[0].upper()
    offset_tok = tokens[2]

    if offset_tok.startswith("+"):
        try:
            offset = int(offset_tok[1:])
        except ValueError:
            return None
        start = offset + 1          # 0-based offset → 1-based position
        if len(tokens) < 5:
            return None
        try:
            length = int(tokens[3])
        except ValueError:
            return None
        ftype = tokens[4].upper() if len(tokens) > 4 else ""
        dec_idx = 5
    else:
        start = prev_end + 1 if prev_end > 0 else 1
        try:
            length = int(offset_tok)
        except ValueError:
            return None
        ftype = tokens[3].upper() if len(tokens) > 3 else ""
        dec_idx = 4

    if ftype not in ("N", "A", "P", "B"):
        return None

    decimals, _, occurs, _ = _parse_optional_attrs(tokens, dec_idx)
    return EZTWSSubfield(name=name, start=start, length=length,
                         type=ftype, decimals=decimals, occurs=occurs)


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
    if tokens[1] == "*":
        start = prev_end + 1
    else:
        try:
            start = int(tokens[1])
        except ValueError:
            return None
    decimals, _, occurs, heading = _parse_optional_attrs(tokens, 4)
    return EZTField(name=name, start=start, length=length,
                    type=ftype, decimals=decimals, occurs=occurs, heading=heading)


def scan_ws_fields(content: str) -> Tuple[List[EZTDefine], str]:
    """Scan arbitrary content for WS field declarations and strip them out.

    Recognises DEFINE statements, W-marker fields, and their sub-fields so that
    all WS declarations inside JOB/REPORT blocks are handled by Python, not the LLM.
    Returns (defines, cleaned_content_with_ws_lines_removed).
    """
    defines: List[EZTDefine] = []
    ws_by_name: dict = {}   # name -> EZTDefine, for sub-field parent lookup
    ws_sub_end: dict = {}   # parent_name -> last sub-field end position
    clean_lines: List[str] = []

    for line in content.splitlines():
        line = line[:72]       # cols 73+ are sequence/id area — ignore
        tokens = line.strip().split()
        if not tokens:
            clean_lines.append(line)
            continue

        d = _parse_define(tokens)
        if d:
            defines.append(d)
            ws_by_name[d.name] = d
            ws_sub_end[d.name] = 0
            continue

        ws = _parse_ws_field(tokens)
        if ws:
            defines.append(ws)
            ws_by_name[ws.name] = ws
            ws_sub_end[ws.name] = 0
            continue

        if len(tokens) > 1 and tokens[1].upper() in ws_by_name:
            parent_name = tokens[1].upper()
            sf = _parse_ws_subfield(tokens, ws_sub_end.get(parent_name, 0))
            if sf:
                ws_by_name[parent_name].subfields.append(sf)
                ws_sub_end[parent_name] = sf.end
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
    ws_by_name: dict = {}   # name -> EZTDefine, for sub-field parent lookup
    ws_sub_end: dict = {}   # parent_name -> last sub-field end position

    for raw_line in source.splitlines():
        line = raw_line[:72]   # cols 73+ are sequence/id area — ignore
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
            if len(tokens) < 2:
                continue
            # For FILE declarations scan the FULL line (cols 1-80) for the org
            # keyword: long file names or spacing may push it past col 72 even
            # though it is still part of the EZT code, not the sequence area.
            # We stop scanning at the first 8-digit all-numeric token (sequence
            # number) so it is never mistaken for a record length.
            org = "DISK"
            rec_len = 0
            _CANONICAL = {"DISK", "VSAM", "TAPE", "PRINTER", "WORK"}
            # Sorted longest-first so "VSAM" is tried before "VS" etc.
            _ALL_KEYS = sorted(
                set(_ORG_ALIASES) | _CANONICAL, key=len, reverse=True
            )
            for tok in raw_line.strip().split()[2:]:
                if len(tok) == 8 and tok.isdigit():
                    break           # reached the sequence-number field — stop
                tok_up = tok.upper()
                # Exact match first, then prefix-match (e.g. "VS00010000")
                canon = _normalise_org(tok_up)
                if canon not in _CANONICAL:
                    for key in _ALL_KEYS:
                        if tok_up.startswith(key) and tok_up[len(key):].isdigit():
                            canon = _normalise_org(key)
                            break
                if canon in _CANONICAL:
                    org = canon
                elif tok.isdigit():
                    rec_len = int(tok)
            current_file = EZTFile(name=tokens[1].upper(), org=org, rec_length=rec_len)
            result.files.append(current_file)
            prev_end = 0

        elif first == "DEFINE":
            current_file = None
            d = _parse_define(tokens)
            if d:
                result.defines.append(d)
                ws_by_name[d.name] = d
                ws_sub_end[d.name] = 0

        elif len(tokens) > 1 and tokens[1].upper() == "W":
            current_file = None
            ws = _parse_ws_field(tokens)
            if ws:
                result.defines.append(ws)
                ws_by_name[ws.name] = ws
                ws_sub_end[ws.name] = 0

        elif len(tokens) > 1 and tokens[1].upper() in ws_by_name:
            parent_name = tokens[1].upper()
            sf = _parse_ws_subfield(tokens, ws_sub_end.get(parent_name, 0))
            if sf:
                ws_by_name[parent_name].subfields.append(sf)
                ws_sub_end[parent_name] = sf.end

        elif current_file is not None:
            f = _parse_field(tokens, prev_end)
            if f:
                current_file.fields.append(f)
                prev_end = f.end

    return result
