"""Assemble per-section COBOL output into a complete, compilable program."""
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from src.parser import EZTSection, SectionType
from src.rule_converter import gen_report_ws
from src.rules import CopybookHook, load_abend_ws, load_copybooks
from src.structured_parser import parse_preamble

# Synthetic keys the converter stashes Python-generated code under.
# Duplicated here (not imported from src.converter) to keep the assembler
# free of import cycles — converter already imports assembler.
_OPEN_CLOSE_KEY  = "open_close:paragraphs"
_READ_PARAS_KEY  = "read:paragraphs"
_READ_EOF_WS_KEY = "read_eof:ws"

# Events Python deterministically generates code for today.  Used to decide
# which copybooks should get a COPY line emitted into the final program;
# events listed in copybooks.yaml whose code Python doesn't yet emit are
# left dormant (the YAML stays declarative for the next phase).
_PY_GENERATED_EVENTS = (
    "file_open_failure", "file_close_failure", "file_read_failure",
)

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

    Comment lines are held back rather than emitted immediately: a comment
    introduces the block that FOLLOWS it (rule_converter puts the
    MOVE-targets block above its 01), so it must live or die with that
    block, not with whichever one happened to precede it.  Emitting them
    inline would drop a surviving block's comment whenever the previous
    block was discarded as a duplicate.
    """
    out: List[str] = []
    seen: set = set()
    skip = False
    pending_comments: List[str] = []
    for line in ws_text.splitlines():
        if _is_proc_comment(line):
            pending_comments.append(line)
            continue
        m = _WS_01_RE.match(line)
        if m:
            name = m.group(1).upper()
            if name == "FILLER":
                skip = False
            elif name in seen:
                skip = True
                pending_comments = []      # discarded block takes them along
                continue
            else:
                seen.add(name)
                skip = False
        if not skip:
            out.extend(pending_comments)
            out.append(line)
        pending_comments = []
    out.extend(pending_comments)
    return "\n".join(out)


# ── Statement-period normalization ──────────────────────────────────────────
#
# COBOL standard style: every complete statement at the top level of a
# paragraph ends with a period (one sentence per statement).  But a period
# is FORBIDDEN in these positions — it would end the whole sentence early
# and orphan the remaining scope words (compile error or broken logic):
#
#   • inside an IF / ELSE body (before ELSE or END-IF)
#   • inside EVALUATE / WHEN branches (before END-EVALUATE)
#   • inside an inline PERFORM UNTIL/VARYING/TIMES body (before END-PERFORM)
#   • inside a verb's conditional phrase — READ ... AT END / NOT AT END,
#     WRITE/START/DELETE ... INVALID KEY, arithmetic ON SIZE ERROR,
#     CALL ... ON EXCEPTION / OVERFLOW — the period belongs after END-READ /
#     END-WRITE / END-COMPUTE etc., never inside the phrase
#   • mid-statement, when the statement continues on the next line
#     (e.g.  MOVE X  /  TO Y)
#
# Strategy: walk the procedure text tracking (a) nesting depth of the
# reliable delimited scopes (IF, EVALUATE, inline PERFORM — the prompt
# mandates their END-x terminators) and (b) a "pending closer" flag for
# verbs sitting in a conditional phrase.  A period is appended to a line
# only when it sits at depth 0, no closer is pending, the line doesn't
# start with a clause keyword or end with a connector token, and the NEXT
# code line clearly starts a new sentence (a COBOL verb or a paragraph
# header).  When in doubt, no period is added — the existing
# _ensure_period_before_paragraphs backstop still guarantees the one
# mandatory period per paragraph.

_VERB_SENTENCE_STARTERS = frozenset("""
    ACCEPT ADD CALL CLOSE COMPUTE CONTINUE DELETE DISPLAY DIVIDE
    EVALUATE EXIT GO GOBACK IF INITIALIZE INSPECT MERGE MOVE MULTIPLY
    OPEN PERFORM READ RELEASE RETURN REWRITE SEARCH SET SORT START STOP
    STRING SUBTRACT UNSTRING WRITE
""".split())

# A line whose FIRST token is one of these is a clause fragment, never a
# complete statement (ELSE, WHEN branches, AT END / INVALID KEY phrases...).
_CLAUSE_FIRST_TOKENS = frozenset("""
    ELSE WHEN AT NOT INVALID ON THEN OTHER
""".split())

# A line whose LAST token is one of these is mid-statement — the operand
# list or clause continues on the following line.
_CONTINUATION_LAST_TOKENS = frozenset("""
    TO FROM INTO BY GIVING UNTIL VARYING THRU THROUGH AND OR OF IN IS
    WITH AFTER BEFORE ADVANCING DELIMITED REPLACING CONVERTING TALLYING
    USING REFERENCE CONTENT KEY UPON FUNCTION ALL = > < >= <= + - * /
""".split())

_COND_PHRASE_RE = re.compile(
    r"\b(?:AT\s+END|INVALID\s+KEY|SIZE\s+ERROR|ON\s+OVERFLOW|"
    r"ON\s+EXCEPTION|NO\s+DATA|END-OF-PAGE)\b")

# END-x closers for the conditional-phrase verbs (not IF/EVALUATE/PERFORM,
# which are depth-tracked separately).
_PHRASE_CLOSER_RE = re.compile(
    r"\bEND-(?:READ|WRITE|REWRITE|DELETE|START|RETURN|CALL|COMPUTE|ADD|"
    r"SUBTRACT|MULTIPLY|DIVIDE|STRING|UNSTRING|SEARCH|ACCEPT|DISPLAY)\b")

_INLINE_PERFORM_RE = re.compile(
    r"\bPERFORM\s+(?:WITH\s+TEST\b|UNTIL\b|VARYING\b|\d+\s+TIMES\b)")

_LINE_TOKEN_RE = re.compile(r"[A-Z][A-Z0-9-]*|\S")


def _is_proc_comment(line: str) -> bool:
    return (len(line) > 6 and line[6] == "*") or line.lstrip().startswith("*")


def _first_code_token(line: str) -> str:
    m = re.search(r"[A-Z][A-Z0-9-]*", _blank_literals(line.upper()))
    return m.group(0) if m else ""


def add_statement_periods(cobol: str) -> str:
    """Append the standard end-of-statement period to complete top-level
    statements, never inside IF/ELSE, EVALUATE, inline PERFORM, or a verb's
    conditional phrase (AT END / INVALID KEY / ON SIZE ERROR / ...).
    """
    lines = cobol.splitlines()

    # Index of the next code line (skipping blanks + comments) per line.
    next_code: List[Optional[int]] = [None] * len(lines)
    nxt: Optional[int] = None
    for i in range(len(lines) - 1, -1, -1):
        next_code[i] = nxt
        if lines[i].strip() and not _is_proc_comment(lines[i]):
            nxt = i

    depth = 0
    pending = False   # inside a conditional phrase awaiting its END-x
    out: List[str] = []

    for i, line in enumerate(lines):
        if not line.strip() or _is_proc_comment(line):
            out.append(line)
            continue
        if _PARA_DEF_RE.match(line):
            depth, pending = 0, False   # paragraph boundary — hard resync
            out.append(line)
            continue

        u = _blank_literals(line.upper())
        toks = _LINE_TOKEN_RE.findall(u)

        opens = sum(1 for t in toks if t in ("IF", "EVALUATE"))
        opens += len(_INLINE_PERFORM_RE.findall(u))
        closes = sum(1 for t in toks
                     if t in ("END-IF", "END-EVALUATE", "END-PERFORM"))
        depth = max(depth + opens - closes, 0)

        if _PHRASE_CLOSER_RE.search(u):
            pending = False
        elif _COND_PHRASE_RE.search(u):
            pending = True

        stripped = line.rstrip()
        add = (
            not stripped.endswith(".")
            and depth == 0
            and not pending
            and toks
            and toks[0] not in _CLAUSE_FIRST_TOKENS
            and toks[-1] not in _CONTINUATION_LAST_TOKENS
            and not stripped.endswith(",")
        )
        if add:
            ni = next_code[i]
            if ni is None:
                pass                       # last code line — period it
            elif _PARA_DEF_RE.match(lines[ni]):
                pass                       # next is a paragraph header
            elif _first_code_token(lines[ni]) in _VERB_SENTENCE_STARTERS:
                pass                       # next line starts a new sentence
            else:
                add = False                # continuation / clause — no period
        out.append(stripped + "." if add else line)

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


def _para_def_names(cobol: str) -> Tuple[str, ...]:
    """Every Area-A paragraph-definition name in `cobol` (headers only).

    Used to learn the dynamic READ-<FILE> / READ-<FILE>-EXIT names Python
    generated so a duplicate the LLM emits can be stripped — these names
    are file-derived and can't be listed statically like _PY_OWNED_PARAS.
    """
    return tuple(m.group(1).upper() for m in _PARA_DEF_RE.finditer(cobol))


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


def gen_missing_var_ws(proc_text: str, data_text: str,
                       extra_declared: Optional[set] = None) -> str:
    """Emit WS declarations for identifiers the procedure code references
    but the DATA DIVISION never declares.  Returns "" when nothing is missing.

    `extra_declared` names identifiers that ARE declared but not visible in
    `data_text` — typically items a COPY member provides (e.g. WS-ABEND-*
    from ERRDATA).  They are treated as declared so they are never
    auto-declared here (which would duplicate the copybook's declaration).
    """
    if not proc_text.strip():
        return ""
    declared, pics = _collect_declared(data_text)
    if extra_declared:
        declared |= {n.upper() for n in extra_declared}
    paragraphs = _collect_paragraph_names(proc_text)
    evidence = _gather_evidence(proc_text, declared, paragraphs, pics)
    if not evidence:
        return ""

    lines: List[str] = []
    for name in sorted(evidence):
        e = evidence[name]
        if e.subscripted:
            # Can't size a subscripted item (OCCURS count unknown) — skip it;
            # it must be declared manually.
            continue
        pic, value = _decide_pic(e, pics)
        suffix = f"{pic} {value}".strip()
        lines.append(f"       01  {name:<33} {suffix}.")

    return "\n".join(lines)


def _active_copybook_events(converted: Dict[str, str]) -> set:
    """Copybook events whose Python-generated code is present in `converted`.

    Only wired events count.  Today that is file_open_failure /
    file_close_failure, active iff the converter produced OPEN-FILES.
    """
    active: set = set()
    if converted.get(_OPEN_CLOSE_KEY):
        active.update({"file_open_failure", "file_close_failure"})
    if converted.get(_READ_PARAS_KEY):
        active.add("file_read_failure")
    return active


# MOVE target on a before_perform statement: the name after the final TO.
_BEFORE_PERFORM_TARGET_RE = re.compile(
    r"\bTO\s+([A-Z][A-Z0-9-]*)\s*$", re.IGNORECASE)


def _copybook_provided_names(
    hooks: Dict[str, CopybookHook],
    converted: Dict[str, str],
) -> set:
    """Identifiers an active hook MOVEs into and whose declaration lives in
    that hook's copy_ws copybook (e.g. WS-ABEND-* in ERRDATA).

    These are declared — just not visible to the in-program DATA DIVISION
    scanner — so gen_missing_var_ws must treat them as declared and never
    auto-declare them (which would duplicate the copybook's version).
    """
    names: set = set()
    for event in _active_copybook_events(converted):
        hook = hooks.get(event)
        if hook is None or not hook.copy_ws:
            continue
        for stmt in hook.before_perform:
            m = _BEFORE_PERFORM_TARGET_RE.search(stmt.strip())
            if m:
                names.add(m.group(1).upper())
    return names


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
    active = _active_copybook_events(converted)
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

    # Python-generated READ-<FILE> paragraphs + their WS-<FILE>-EOF flags
    # (one per INPUT file).  Paragraphs are spliced into PROCEDURE alongside
    # OPEN/CLOSE; the EOF flags go straight into WORKING-STORAGE.
    read_paras_text = (converted.get(_READ_PARAS_KEY) or "").strip("\n")
    read_eof_ws = (converted.get(_READ_EOF_WS_KEY) or "").strip("\n")
    if read_eof_ws:
        ws_parts.append(read_eof_ws)

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
        # Defensive: if Python already emitted OPEN-FILES / CLOSE-FILES /
        # READ-<FILE>, drop any duplicate the LLM produced.  PERFORM
        # references stay intact (they don't match _PARA_DEF_RE).  READ-<FILE>
        # names are file-derived, so read them off the generated text.
        if open_close_text:
            clean_proc = _strip_paragraphs(clean_proc, _PY_OWNED_PARAS)
        if read_paras_text:
            clean_proc = _strip_paragraphs(
                clean_proc, _para_def_names(read_paras_text)
            )
        # Rename any paragraph whose bare name collides with a COBOL
        # reserved word (INITIAL, TERMINATE, etc.) — both definitions
        # and PERFORM references are rewritten in lockstep.
        clean_proc = _rename_reserved_paragraphs(clean_proc)
        # COBOL has no IS INTEGER class test — rewrite to IS NUMERIC.
        clean_proc = _fix_integer_class_test(clean_proc)
        # Standard statement periods: one per complete top-level statement,
        # never inside IF/ELSE, EVALUATE, inline PERFORM, or a conditional
        # phrase (AT END / INVALID KEY / ON SIZE ERROR / ...).
        clean_proc = add_statement_periods(clean_proc)
        # Ensure each paragraph's last statement ends with a period so the
        # next paragraph header doesn't get parsed as part of it.
        clean_proc = _ensure_period_before_paragraphs(clean_proc)
        if clean_proc:
            procedure_parts.append(clean_proc)

    # Append the Python-generated OPEN-FILES / CLOSE-FILES and READ-<FILE>
    # paragraphs after the LLM's logic.  Order doesn't matter for COBOL
    # paragraph resolution, but placing them after MAIN-PROCESS keeps the
    # program's narrative flow.
    if open_close_text:
        procedure_parts.append(open_close_text)
    if read_paras_text:
        procedure_parts.append(read_paras_text)

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
    if open_close_text or read_paras_text:
        abend_ws_items = load_abend_ws()
        if abend_ws_items:
            ws_parts.append("\n".join(abend_ws_items))

    # Auto-declare any identifier the procedure code references but the
    # DATA DIVISION never declares (PIC inferred from usage).  Runs on the
    # FINAL procedure text — after reserved-word renames and paragraph
    # stripping — so the names scanned are the names that will compile.
    # Names an active copybook hook provides (WS-ABEND-* from ERRDATA etc.)
    # are passed as extra_declared so they aren't auto-declared here — that
    # would duplicate the copybook's own declaration.
    proc_text_all = "\n\n".join(procedure_parts)
    if proc_text_all.strip():
        data_text_all = "\n".join(
            file_control_parts + file_section_parts + ws_parts
        )
        missing_ws = gen_missing_var_ws(
            proc_text_all, data_text_all,
            extra_declared=_copybook_provided_names(hooks, converted),
        )
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
    cobol = align_keywords(cobol)          # snap PIC/VALUE/TO/THRU to columns
    return _enforce_col_limit(cobol)


# ── Keyword column alignment ─────────────────────────────────────────────────
#
# Shop convention: certain keywords always start in a fixed column so a data
# item's PIC/VALUE and a statement's TO/THRU line up vertically for reading.
# Targets are 1-indexed columns, chosen per DIVISION:
#
#   TO → 42, IS → 42     (ENVIRONMENT / FILE-CONTROL — the SELECT clauses:
#                         ASSIGN TO, ORGANIZATION IS, ACCESS MODE IS,
#                         FILE STATUS IS, RECORD KEY IS.)
#   PIC  → 45, VALUE → 60 (DATA DIVISION data-item lines.)
#   THRU → 40, TO → 42   (PROCEDURE DIVISION — PERFORM ... THRU,
#                         MOVE/ADD ... TO.  ASSIGN TO / GO TO are excluded
#                         here — those are not MOVE targets.)
#
# For each keyword we left-pad with spaces so it starts on its column.  When
# the text before it already reaches the column (no room for even one
# separating space), the keyword and everything after it move to a fresh
# continuation line that begins at the target column — always valid COBOL
# (a data entry / statement may span lines), never a truncated identifier.
# Any resulting line past column 72 is folded afterwards by _enforce_col_limit.

_ALIGN_ENV  = (("TO", 42), ("IS", 42))         # ENVIRONMENT / FILE-CONTROL
_ALIGN_DATA = (("PIC", 45), ("VALUE", 60))     # DATA DIVISION keywords
_ALIGN_PROC = (("THRU", 40), ("TO", 42))       # PROCEDURE DIVISION keywords

# In PROCEDURE, a TO preceded by one of these is NOT a MOVE/ADD target — leave
# it alone.  (In FILE-CONTROL we DO align ASSIGN TO, so this is proc-only.)
_TO_SKIP_PREV = {"GO", "ASSIGN"}


def _find_kw_idx(masked: str, kw: str, skip_prev: bool = False) -> int:
    """Index of the first `kw` token in `masked` (literals already blanked),
    matched only as a whitespace-delimited word so it never fires inside a
    hyphenated name (MOVE-TO-X) or a longer word (INTO / PICTURE).  Returns
    -1 if absent.  When `skip_prev`, a TO after ASSIGN / GO is skipped."""
    for m in re.finditer(r"(?<=\s)" + kw + r"(?=\s|$)", masked):
        idx = m.start()
        if kw == "TO" and skip_prev:
            before = masked[:idx].rstrip().rsplit(None, 1)
            if before and before[-1].upper() in _TO_SKIP_PREV:
                continue
        return idx
    return -1


def _align_one_line(line: str, targets: Tuple[Tuple[str, int], ...],
                    skip_to_prev: bool = False) -> List[str]:
    """Align each (keyword, col) on `line`, returning one or more physical
    lines (extra lines appear only when a keyword had to move down)."""
    lines = [line.rstrip()]
    for kw, col in targets:
        tail = lines[-1]                       # the keyword lives on the tail
        idx = _find_kw_idx(_blank_literals(tail), kw, skip_prev=skip_to_prev)
        if idx < 0:
            continue
        prefix = tail[:idx].rstrip()
        rest = tail[idx:]                      # keyword + everything after it
        target0 = col - 1                      # 0-based start column
        if len(prefix) <= target0 - 1:         # room for ≥1 separating space
            lines[-1] = prefix + " " * (target0 - len(prefix)) + rest
        else:                                   # no room → move down a line
            lines[-1] = prefix
            lines.append(" " * target0 + rest)
    return lines


def align_keywords(cobol: str) -> str:
    """Snap TO/IS (FILE-CONTROL), PIC/VALUE (DATA) and TO/THRU (PROCEDURE) to
    their shop columns, per division."""
    out: List[str] = []
    div = "env"                                # ENVIRONMENT/IDENTIFICATION first
    for line in cobol.splitlines():
        if re.match(r"\s*DATA\s+DIVISION", line, re.IGNORECASE):
            div = "data"
        elif re.match(r"\s*PROCEDURE\s+DIVISION", line, re.IGNORECASE):
            div = "proc"
        if not line.strip() or _is_proc_comment(line):
            out.append(line)
            continue
        if div == "proc":
            out.extend(_align_one_line(line, _ALIGN_PROC, skip_to_prev=True))
        elif div == "data":
            out.extend(_align_one_line(line, _ALIGN_DATA))
        else:                                  # env (only FILE-CONTROL has TO/IS)
            out.extend(_align_one_line(line, _ALIGN_ENV))
    return "\n".join(out)


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
