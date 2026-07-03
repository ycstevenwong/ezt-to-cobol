"""Assemble per-section COBOL output into a complete, compilable program."""
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from src.parser import EZTSection, SectionType
from src.rule_converter import gen_report_ws
from src.rules import CopybookHook, load_abend_ws, load_copybooks
from src.structured_parser import parse_preamble

# Synthetic key the converter stashes Python-generated OPEN/CLOSE paragraphs
# under.  Duplicated here (not imported from src.converter) to keep the
# assembler free of import cycles — converter already imports assembler.
_OPEN_CLOSE_KEY = "open_close:paragraphs"

# Events Python deterministically generates code for today.  Used to decide
# which copybooks should get a COPY line emitted into the final program;
# events listed in copybooks.yaml whose code Python doesn't yet emit are
# left dormant (the YAML stays declarative for the next phase).
_PY_GENERATED_EVENTS = ("file_open_failure", "file_close_failure")

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


_WS_01_RE = re.compile(r"^\s*01\s+([A-Z][A-Z0-9-]*)", re.IGNORECASE)


def _dedupe_ws_items(ws_text: str) -> str:
    """Drop duplicate 01-level items in the WORKING-STORAGE block.

    When two declarations share the same 01-level name the compiler rejects
    the program.  Keep the FIRST occurrence (rule-generated identifiers
    that the LLM was told to use) and discard subsequent duplicates along
    with their subordinate 05/10/... lines.  FILLER 01 items are exempt —
    they intentionally repeat.
    """
    out: List[str] = []
    seen: set = set()
    skip = False
    for line in ws_text.splitlines():
        m = _WS_01_RE.match(line)
        if m:
            name = m.group(1).upper()
            if name == "FILLER":
                skip = False
            elif name in seen:
                skip = True
                continue
            else:
                seen.add(name)
                skip = False
        if not skip:
            out.append(line)
    return "\n".join(out)


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


# Paragraph names Python emits and the LLM is told NOT to redeclare.
# When the LLM ignores that, _strip_paragraphs removes its definition
# (header + body) so we don't end up with two `OPEN-FILES.` paragraphs —
# which the COBOL compiler rejects.  PERFORM references stay untouched.
_PY_OWNED_PARAS = (
    "OPEN-FILES",
    "OPEN-FILES-EXIT",
    "CLOSE-FILES",
    "CLOSE-FILES-EXIT",
)


def _strip_paragraphs(cobol: str, names: Tuple[str, ...]) -> str:
    """Delete each named paragraph definition from procedure code.

    Walks the source line-by-line: when an Area-A `<name>.` header for any
    of `names` is encountered, drop lines until the next Area-A paragraph
    header (or end of text).  Multiple definitions of the same name are
    all removed.  Lines that merely *reference* the name (e.g.
    `PERFORM OPEN-FILES`) are untouched — they don't match _PARA_DEF_RE.
    """
    targets = {n.upper() for n in names}
    out: List[str] = []
    skip = False
    for line in cobol.splitlines():
        m = _PARA_DEF_RE.match(line)
        if m:
            skip = m.group(1).upper() in targets
            if skip:
                continue
        if not skip:
            out.append(line)
    return "\n".join(out)


# ── Auto-declaration of missing variables ────────────────────────────────────
#
# Even with the identifier allow-list in the prompt, the LLM sometimes
# references a variable it never declared, and the COBOL compile fails on
# an undefined data name.  Instead of letting that happen, scan the final
# procedure text for identifiers that are neither declared in the DATA
# DIVISION nor paragraph names / reserved words, infer a PIC from how each
# one is USED, and emit the declarations as an extra WS block flagged for
# human review.
#
# PIC inference, in priority order per identifier:
#   1. ACCEPT x FROM DATE/TIME/DAY        → fixed PIC (9(8) / 9(6) / 9(5))
#   2. used inside a subscript (T(x))     → index: S9(4) COMP VALUE 1
#   3. arithmetic target (ADD/COMPUTE/…)  → numeric; widened from a declared
#      numeric partner's digits when one appears in the same statement,
#      else S9(9)V9(2) COMP-3
#   4. MOVE/compare partner with a declared field → copy the partner's PIC
#   5. MOVE/compare with a quoted literal → PIC X(longest literal)
#   6. MOVE/compare with a numeric literal → PIC 9(digits)[V9(dec)]
#   7. no evidence at all                 → PIC X(20) (flagged in a comment)
#
# Oversizing is safe (COBOL pads); undersizing truncates silently, so the
# numeric defaults lean generous.  Subscripted references are NOT declared
# (the OCCURS size is unknowable) — a review comment is emitted instead.

# Words that may appear in operand position but are never user data names.
_RESERVED_WORDS = frozenset("""
    ACCEPT ADD ADVANCING AFTER ALL ALSO AND ARE AT BEFORE BY CALL CLOSE
    COMP COMP-3 COMPUTE CONTENT CONTINUE CONVERTING CORR CORRESPONDING
    COUNT DATE DAY DAY-OF-WEEK DELETE DELIMITED DELIMITER DEPENDING
    DISPLAY DIVIDE DOWN DUPLICATES ELSE END EQUAL ERROR EVALUATE
    EXCEPTION EXIT EXTEND FALSE FIRST FROM FUNCTION GIVING GO GOBACK
    GREATER HIGH-VALUE HIGH-VALUES I-O IF IN INITIAL INITIALIZE INPUT
    INSPECT INTO INVALID IS JUST JUSTIFIED KEY LEADING LESS LINE LINES
    LOCK LOW-VALUE LOW-VALUES MERGE MOVE MULTIPLY NEGATIVE NEXT NO NOT
    NUMERIC ALPHABETIC ALPHANUMERIC OF ON OPEN OR OTHER OUTPUT OVERFLOW
    PAGE PERFORM POINTER POSITIVE QUOTE QUOTES READ RECORD REFERENCE
    RELEASE REMAINDER REPLACING RETURN RETURN-CODE REVERSED REWIND
    REWRITE ROUNDED RUN SEARCH SENTENCE SET SIZE SORT SPACE SPACES START
    STOP STRING SUBTRACT TALLYING TEST THAN THEN THRU THROUGH TIME TIMES
    TO TRUE UNSTRING UNTIL UP UPON USING VALUE VARYING WHEN WITH WRITE
    ZERO ZEROES ZEROS YYYYMMDD YYYYDDD
""".split())

_IDENT_TOKEN_RE   = re.compile(r"[A-Z][A-Z0-9-]*")
_QUOTED_RE        = re.compile(r"'[^']*'|\"[^\"]*\"")
_NUM_LITERAL_RE   = re.compile(r"^[+-]?\d+(\.\d+)?$")

# Declared-name harvesting from the assembled DATA DIVISION text.
_DECL_NAME_RE  = re.compile(r"^\s*\d{2}\s+([A-Z][A-Z0-9-]*)", re.IGNORECASE)
_FD_SELECT_RE  = re.compile(r"^\s*(?:FD|SELECT)\s+([A-Z][A-Z0-9-]*)",
                            re.IGNORECASE)
_PIC_CLAUSE_RE = re.compile(r"\bPIC(?:TURE)?\s+(?:IS\s+)?(\S+)", re.IGNORECASE)

# Paragraph references — PERFORM x [THRU y], GO TO x.  These identifiers
# are code labels, never data items; they must not be auto-declared.
_PERFORM_REF_RE = re.compile(
    r"\b(?:PERFORM|THRU|THROUGH)\s+([A-Z][A-Z0-9-]*)", re.IGNORECASE)
_GO_TO_REF_RE   = re.compile(r"\bGO\s+TO\s+([A-Z][A-Z0-9-]*)", re.IGNORECASE)

_ACCEPT_FROM_RE = re.compile(
    r"\bACCEPT\s+([A-Z][A-Z0-9-]*)\s+FROM\s+(DATE\s+YYYYMMDD|DATE|TIME|DAY)\b",
    re.IGNORECASE)
_ACCEPT_PICS = {"DATE YYYYMMDD": "9(8)", "DATE": "9(6)",
                "TIME": "9(8)", "DAY": "9(5)"}

_MOVE_STMT_RE = re.compile(r"\bMOVE\s+(\S+)\s+TO\s+(.+?)(?:\.\s*$|$)",
                           re.IGNORECASE)
_COMPARE_RE = re.compile(
    r"(\S+)\s*(?:IS\s+)?(?:NOT\s+)?(?:=|>=|<=|>|<)\s*(\S+)", re.IGNORECASE)

_ARITH_VERBS_RE = re.compile(
    r"\b(?:ADD|SUBTRACT|MULTIPLY|DIVIDE|COMPUTE)\b", re.IGNORECASE)

_EDITED_PIC_CHARS = set("Z*,.$+-B/")


@dataclass
class _VarEvidence:
    """Accumulated usage evidence for one undeclared identifier."""
    arith:       bool = False
    alpha_len:   int = 0
    num_int:     int = 0
    num_dec:     int = 0
    partners:    List[str] = field(default_factory=list)
    special_pic: Optional[str] = None
    subscripted: bool = False
    index:       bool = False   # appears INSIDE a subscript → numeric index


def _blank_literals(line: str) -> str:
    """Replace quoted literals with spaces so their content isn't scanned."""
    return _QUOTED_RE.sub(lambda m: " " * len(m.group(0)), line)


def _is_numeric_pic(pic: str) -> bool:
    body = pic.upper().replace("(", "").replace(")", "")
    return "9" in body and all(c in "S9V0123456789" for c in body)


def _pic_digit_counts(pic: str) -> Tuple[int, int]:
    """Return (integer_digits, decimal_digits) for a numeric PIC string."""
    int_part, _, dec_part = pic.upper().partition("V")

    def count(part: str) -> int:
        total = 0
        for m in re.finditer(r"9(?:\((\d+)\))?", part):
            total += int(m.group(1)) if m.group(1) else 1
        return total

    return count(int_part), count(dec_part)


def _collect_declared(data_text: str) -> Tuple[set, Dict[str, str]]:
    """Scan assembled DATA DIVISION text for declared names and their PICs.

    Returns (declared_names, {name: pic_string}).  File names from FD /
    SELECT lines count as declared (they appear in OPEN / READ / CLOSE).
    """
    declared: set = set()
    pics: Dict[str, str] = {}
    for line in data_text.splitlines():
        m = _DECL_NAME_RE.match(line) or _FD_SELECT_RE.match(line)
        if not m:
            continue
        name = m.group(1).upper()
        declared.add(name)
        pm = _PIC_CLAUSE_RE.search(line)
        if pm and name not in pics:
            pic = pm.group(1).upper()
            if pic.endswith("."):
                pic = pic[:-1]          # statement period, not part of the PIC
            if re.search(r"\bCOMP-3\b", line, re.IGNORECASE):
                pic += " COMP-3"
            elif re.search(r"\bCOMP\b", line, re.IGNORECASE):
                pic += " COMP"
            pics[name] = pic
    declared.add("RETURN-CODE")         # special register, always available
    return declared, pics


def _collect_paragraph_names(proc_text: str) -> set:
    """All code labels: paragraph definitions plus PERFORM/THRU/GO TO refs."""
    names: set = set()
    for m in _PARA_DEF_RE.finditer(proc_text):
        names.add(m.group(1).upper())
    for m in _PERFORM_REF_RE.finditer(proc_text):
        names.add(m.group(1).upper())
    for m in _GO_TO_REF_RE.finditer(proc_text):
        names.add(m.group(1).upper())
    return names


def _is_candidate(tok: str, declared: set, paragraphs: set) -> bool:
    return (tok not in _RESERVED_WORDS
            and not tok.startswith("END-")
            and tok not in declared
            and tok not in paragraphs)


def _operand_evidence(ev: _VarEvidence, operand: str, declared: set,
                      pics: Dict[str, str]) -> None:
    """Fold one partner operand (literal / figurative / field) into evidence."""
    op = operand.rstrip(".").rstrip(",")
    if op.startswith(("'", '"')):
        ev.alpha_len = max(ev.alpha_len, max(len(op) - 2, 1))
    elif _NUM_LITERAL_RE.match(op):
        digits = op.lstrip("+-")
        int_p, _, dec_p = digits.partition(".")
        ev.num_int = max(ev.num_int, len(int_p))
        ev.num_dec = max(ev.num_dec, len(dec_p))
    elif op in ("SPACE", "SPACES"):
        ev.alpha_len = max(ev.alpha_len, 1)
    elif op in ("ZERO", "ZEROS", "ZEROES"):
        ev.num_int = max(ev.num_int, 1)
    elif op.upper() in declared and op.upper() in pics:
        ev.partners.append(op.upper())


def _gather_evidence(proc_text: str, declared: set,
                     paragraphs: set, pics: Dict[str, str],
                     ) -> Dict[str, _VarEvidence]:
    """One pass over procedure text collecting usage evidence per unknown."""
    evidence: Dict[str, _VarEvidence] = {}

    def ev(name: str) -> _VarEvidence:
        return evidence.setdefault(name, _VarEvidence())

    for raw in proc_text.splitlines():
        stripped = raw.lstrip()
        if not stripped or stripped.startswith("*") \
                or (len(raw) > 6 and raw[6] == "*"):
            continue
        line = raw.upper()
        blanked = _blank_literals(line)

        # Candidate identifiers on this line (literals blanked out).
        # Track subscripted references — they can't be safely declared.
        prev_tok = ""
        line_candidates: List[str] = []
        for m in _IDENT_TOKEN_RE.finditer(blanked):
            tok = m.group(0)
            if prev_tok == "FUNCTION":       # intrinsic name, not a data item
                prev_tok = tok
                continue
            prev_tok = tok
            if not _is_candidate(tok, declared, paragraphs):
                continue
            rest = blanked[m.end():].lstrip()
            if rest.startswith("("):
                ev(tok).subscripted = True
                # Identifiers inside the subscript are index variables —
                # they must be numeric regardless of other evidence.
                inner = rest[1:rest.index(")")] if ")" in rest else rest[1:]
                for idx_tok in _IDENT_TOKEN_RE.findall(inner):
                    if _is_candidate(idx_tok, declared, paragraphs):
                        ev(idx_tok).index = True
                continue
            line_candidates.append(tok)
            ev(tok)                          # register even without evidence

        if not line_candidates and not evidence:
            continue

        # ACCEPT x FROM DATE/TIME/DAY → exact PIC known.
        am = _ACCEPT_FROM_RE.search(line)
        if am:
            name = am.group(1).upper()
            if name in evidence:
                key = " ".join(am.group(2).upper().split())
                evidence[name].special_pic = _ACCEPT_PICS.get(key)

        # MOVE <src> TO <targets...>
        mm = _MOVE_STMT_RE.search(line)
        if mm:
            src = mm.group(1).upper()
            # Re-read the source token from the ORIGINAL line so quoted
            # literal content survives (blanked copy loses it).
            src_orig = _MOVE_STMT_RE.search(raw.upper()).group(1)
            targets = [t for t in _IDENT_TOKEN_RE.findall(
                       _blank_literals(mm.group(2).upper()))]
            for tgt in targets:
                if tgt in line_candidates:
                    _operand_evidence(ev(tgt), src_orig, declared, pics)
            if src in line_candidates:
                for tgt in targets:
                    if tgt in declared and tgt in pics:
                        ev(src).partners.append(tgt)

        # Arithmetic statements: every candidate on the line is numeric;
        # declared numeric fields on the line become sizing partners.
        if _ARITH_VERBS_RE.search(blanked):
            declared_partners = [
                t for t in _IDENT_TOKEN_RE.findall(blanked)
                if t in declared and t in pics and _is_numeric_pic(
                    pics[t].replace(" COMP-3", "").replace(" COMP", ""))
            ]
            for cand in line_candidates:
                e = ev(cand)
                e.arith = True
                e.partners.extend(declared_partners)
                for lit in re.findall(r"\b\d+(?:\.\d+)?\b", blanked):
                    _operand_evidence(e, lit, declared, pics)

        # Comparisons (IF / WHEN / PERFORM UNTIL ...): pair each side.
        for cm in _COMPARE_RE.finditer(raw.upper()):
            left, right = cm.group(1).upper(), cm.group(2)
            l_name = left.rstrip(".,)")
            r_name = right.upper().rstrip(".,)")
            if l_name in line_candidates:
                _operand_evidence(ev(l_name), right, declared, pics)
            if r_name in line_candidates:
                _operand_evidence(ev(r_name), left, declared, pics)

    return evidence


def _decide_pic(e: _VarEvidence, pics: Dict[str, str]) -> Tuple[str, str]:
    """Return (pic_clause, value_clause) for one undeclared identifier."""
    if e.special_pic:
        return f"PIC {e.special_pic}", "VALUE ZERO"

    if e.index:
        # Subscript index — binary halfword, started at 1 so an immediate
        # use before any SET/MOVE stays within table bounds.
        return "PIC S9(4) COMP", "VALUE 1"

    partner_pics = [pics[p] for p in e.partners if p in pics]

    if e.arith:
        for ppic in partner_pics:
            bare = ppic.replace(" COMP-3", "").replace(" COMP", "")
            if _is_numeric_pic(bare):
                ints, decs = _pic_digit_counts(bare)
                ints = min(ints + 2, 18 - decs)   # headroom for totals
                dec_part = f"V9({decs})" if decs else ""
                return f"PIC S9({ints}){dec_part} COMP-3", "VALUE ZERO"
        if e.num_int:
            dec_part = f"V9({e.num_dec})" if e.num_dec else ""
            return (f"PIC S9({max(e.num_int + 2, 4)}){dec_part} COMP-3",
                    "VALUE ZERO")
        return "PIC S9(9)V9(2) COMP-3", "VALUE ZERO"

    if partner_pics:
        ppic = partner_pics[0]
        bare = ppic.replace(" COMP-3", "").replace(" COMP", "")
        if _is_numeric_pic(bare):
            return f"PIC {ppic}", "VALUE ZERO"
        if any(c in _EDITED_PIC_CHARS for c in bare):
            return f"PIC {bare}", ""          # edited PIC — no VALUE allowed
        return f"PIC {bare}", "VALUE SPACES"

    if e.alpha_len:
        return f"PIC X({e.alpha_len})", "VALUE SPACES"
    if e.num_int:
        dec_part = f"V9({e.num_dec})" if e.num_dec else ""
        return f"PIC 9({e.num_int}){dec_part}", "VALUE ZERO"

    return "PIC X(20)", "VALUE SPACES"        # no usable evidence


def gen_missing_var_ws(proc_text: str, data_text: str) -> str:
    """Emit WS declarations for identifiers the procedure code references
    but the DATA DIVISION never declares.  Returns "" when nothing is missing.
    """
    if not proc_text.strip():
        return ""
    declared, pics = _collect_declared(data_text)
    paragraphs = _collect_paragraph_names(proc_text)
    evidence = _gather_evidence(proc_text, declared, paragraphs, pics)
    if not evidence:
        return ""

    lines: List[str] = [
        "      * AUTO-DECLARED: the following identifiers are referenced by",
        "      * the generated logic but were missing from the DATA DIVISION.",
        "      * PICs are inferred from usage -- review before compiling.",
    ]
    emitted = False
    for name in sorted(evidence):
        e = evidence[name]
        if e.subscripted:
            lines.append(f"      * {name} is referenced with a subscript --")
            lines.append("      * OCCURS size unknown; declare it manually.")
            continue
        pic, value = _decide_pic(e, pics)
        suffix = f"{pic} {value}".strip()
        lines.append(f"       01  {name:<33} {suffix}.")
        emitted = True

    if not emitted and len(lines) <= 3:
        return ""
    return "\n".join(lines)


def _resolve_copy_lines(
    hooks: Dict[str, CopybookHook],
    converted: Dict[str, str],
) -> Tuple[List[str], List[str]]:
    """Return (ws_copybooks, procedure_copybooks) — deduped, sorted lists.

    Only hooks whose event has actually been wired into the Python
    generators contribute; an event in copybooks.yaml with no generated
    code stays dormant.  Today the only wired events are file_open_failure
    and file_close_failure (active iff the converter produced OPEN-FILES).
    """
    active: set = set()
    if converted.get(_OPEN_CLOSE_KEY):
        active.update({"file_open_failure", "file_close_failure"})
    ws_set: set = set()
    proc_set: set = set()
    for event in active:
        hook = hooks.get(event)
        if hook is None:
            continue
        if hook.copy_ws:
            ws_set.add(hook.copy_ws)
        if hook.copy_procedure:
            proc_set.add(hook.copy_procedure)
    return sorted(ws_set), sorted(proc_set)


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

    # Python-generated OPEN-FILES / CLOSE-FILES paragraphs (if any).  Held
    # for splicing into procedure_parts AFTER the LLM's logic so MAIN-PROCESS
    # (which PERFORMs them by name) appears first.
    open_close_text = (converted.get(_OPEN_CLOSE_KEY) or "").strip("\n")

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
        # Defensive: if Python already emitted OPEN-FILES / CLOSE-FILES,
        # drop any duplicate the LLM produced despite the prompt telling
        # it not to.  PERFORM references in MAIN-PROCESS stay intact.
        if open_close_text:
            clean_proc = _strip_paragraphs(clean_proc, _PY_OWNED_PARAS)
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

    # Append the Python-generated OPEN-FILES / CLOSE-FILES after the LLM's
    # logic.  Order doesn't matter for COBOL paragraph resolution, but
    # placing them after MAIN-PROCESS keeps the program's narrative flow.
    if open_close_text:
        procedure_parts.append(open_close_text)

    # Resolve which copybook COPY lines to emit per division.  Only events
    # Python actually generated code for contribute to the dedup set; events
    # configured in copybooks.yaml whose code Python doesn't yet emit are
    # ignored here (the YAML stays declarative for later phases).
    hooks = load_copybooks()
    ws_copies, proc_copies = _resolve_copy_lines(hooks, converted)

    # abend_ws — global WS items referenced by before_perform MOVEs
    # (WS-ABEND-CODE / WS-ABEND-MSG / WS-ABEND-STATUS by default).  Emitted
    # only when at least one wired event fires; comment items out of YAML
    # if your copybook already declares them.
    if open_close_text:
        abend_ws_items = load_abend_ws()
        if abend_ws_items:
            ws_parts.append("\n".join(abend_ws_items))

    # Auto-declare any identifier the procedure code references but the
    # DATA DIVISION never declares (PIC inferred from usage).  Runs on the
    # FINAL procedure text — after reserved-word renames and paragraph
    # stripping — so the names scanned are the names that will compile.
    # NOTE: names declared inside COPY members (ERRDATA etc.) are invisible
    # here; _dedupe_ws_items can't help either, so if a copybook already
    # declares one of these the copybook version wins by being COPY'd last.
    proc_text_all = "\n\n".join(procedure_parts)
    if proc_text_all.strip():
        data_text_all = "\n".join(
            file_control_parts + file_section_parts + ws_parts
        )
        missing_ws = gen_missing_var_ws(proc_text_all, data_text_all)
        if missing_ws:
            ws_parts.append(missing_ws)

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
        # Dedupe across every WS chunk (Python-generated + LLM-supplied) so
        # a name declared by rule_converter isn't redeclared by the LLM's
        # WS block (COBOL rejects duplicate 01-level identifiers).
        data_sections.append(_dedupe_ws_items("\n".join(ws_parts)))
    elif not ws_copies:
        data_sections.append("       01 FILLER PIC X.")
    if ws_copies:
        # COPY lines for copybooks that contribute WS items (variables,
        # 01-level declarations, etc.).  Placed AFTER the in-program WS so
        # any name the copybook re-declares wins by being last.
        data_sections.append(
            "\n".join(f"       COPY {name}." for name in ws_copies)
        )
    data_div = "\n".join(data_sections)

    # PROCEDURE DIVISION
    proc_body = "\n\n".join(procedure_parts) if procedure_parts else "           STOP RUN."
    if proc_copies:
        # Paragraph-only copybook(s) at the bottom of PROCEDURE — they
        # define routines (FILE-ERROR-RTN etc.) the body just PERFORMs.
        proc_body += "\n\n" + "\n".join(f"       COPY {name}." for name in proc_copies)
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
