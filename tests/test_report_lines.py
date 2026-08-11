#!/usr/bin/env python3
"""Regression tests for REPORT line parsing and layout generation.

Covers the TITLE/HEADING/LINE/FOOTING directive forms and the MOVE-targets
comment block.  Every case here corresponds to a bug that was found by hand
and silently produced a short or shifted report line — none of them make a
conversion fail loudly, which is why they need to be pinned down.

Run standalone (no dependencies):

    python3 tests/test_report_lines.py

or under pytest if it is installed:

    pytest tests/
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.assembler import _dedupe_ws_items, assemble          # noqa: E402
from src.parser import parse_ezt, SectionType                 # noqa: E402
from src.rule_converter import (                              # noqa: E402
    _DEFAULT_PRINT_WIDTH,
    _move_targets_comment,
    _parse_text_line,
    convert_field_def,
    convert_file_def,
    gen_report_ws,
)
from src.structured_parser import join_continuations, parse_preamble   # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────

PREAMBLE_SRC = """\
FILE CUSTFILE DISK 80
CUSTNO    1   5   N
CUSTNAME  6  30   A
BALANCE  36  10   P 2
STATUS   46   1   A
"""


def _pre():
    return parse_preamble(PREAMBLE_SRC)


def _segments(payload):
    """(text_or_FIELD, gap_before) for each fragment of a directive payload."""
    line = _parse_text_line(payload)
    return [(f.text if f.text is not None else f"<{f.field}>", f.gap_before)
            for f in line.fragments]


def _ws(content, rpt="R"):
    return gen_report_ws(rpt, content, preamble=_pre())


def _layout(ws_text, name):
    """The 05-level lines belonging to the named 01 layout.

    Raises if the layout is absent: a helper that quietly returns [] lets a
    test compare two empty strings and pass without exercising anything —
    which is exactly how the first draft of the trailing-word test managed
    to pass while the bug it targeted was still live.
    """
    out, inside, found = [], False, False
    for line in ws_text.splitlines():
        if re.match(rf"\s+01\s+{re.escape(name)}\s*\.", line):
            inside = found = True
            continue
        if inside:
            if re.match(r"\s+01\s", line) or line.lstrip().startswith("*"):
                break
            out.append(line)
    assert found, f"layout {name!r} not found; have: {_layout_names(ws_text)}"
    return out


def _layout_names(ws_text):
    return [m.group(1) for line in ws_text.splitlines()
            if (m := re.match(r"\s+01\s+(\S+?)\.", line))]


def _width(ws_text, name):
    """Total print columns a layout occupies — must equal the page width."""
    return sum(int(m.group(1))
               for line in _layout(ws_text, name)
               if (m := re.search(r"PIC X\((\d+)\)", line)))


def _rendered(ws_text, name):
    """The printed line: literals inline, runtime fields as '?'."""
    out = ""
    for line in _layout(ws_text, name):
        m = re.search(r"PIC X\((\d+)\)", line)
        if not m:
            continue
        n = int(m.group(1))
        val = re.search(r"VALUE '(.*)'", line)
        if val:
            out += val.group(1).ljust(n)
        elif "VALUE SPACES" in line:
            out += " " * n
        else:
            out += "?" * n
    return out


def _first_col(text):
    """1-based column where the rendered line's content starts (0 if blank)."""
    return len(text) - len(text.lstrip()) + 1 if text.strip() else 0


# ── directive parsing: the +N gap spacer ─────────────────────────────────

def test_gap_canonical():
    assert _segments("01 'TES CENTER' +19 'AGING REPORT'") == [
        ("TES CENTER", None), ("AGING REPORT", 19)]


def test_gap_trailing_plus_variant():
    assert _segments("01 'TES CENTER' +19+ 'AGING REPORT'") == [
        ("TES CENTER", None), ("AGING REPORT", 19)]


def test_gap_spaced_plus_variant():
    assert _segments("01 'TES CENTER' + 19 'AGING REPORT'") == [
        ("TES CENTER", None), ("AGING REPORT", 19)]


def test_gap_with_stray_join_plus():
    assert _segments("01 'TES CENTER' +19 + 'AGING REPORT'") == [
        ("TES CENTER", None), ("AGING REPORT", 19)]


def test_gap_multi_segment():
    assert _segments("01 'TES' +5 'CENTER' +19 'AGING REPORT'") == [
        ("TES", None), ("CENTER", 5), ("AGING REPORT", 19)]


def test_gap_leading():
    assert _segments("01 +19 'LEADING GAP'") == [("LEADING GAP", 19)]


def test_field_inside_chain():
    assert _segments("01 'CUSTOMER: ' +2 CUSTNO +3 'DUE'") == [
        ("CUSTOMER: ", None), ("<CUSTNO>", 2), ("DUE", 3)]


# ── directive parsing: pre-existing forms must not regress ───────────────

def test_pure_col_form():
    line = _parse_text_line("01 COL 1 'LEFT' COL 20 'RIGHT'")
    assert [(f.text, f.col) for f in line.fragments] == [("LEFT", 1), ("RIGHT", 20)]


def test_single_literal_stays_centered():
    line = _parse_text_line("'CUSTOMER BALANCE REPORT'")
    assert not line.fragments
    assert line.text == "CUSTOMER BALANCE REPORT"


def test_col_and_gap_mixed():
    line = _parse_text_line("01 COL 1 'TES' +19 'AGING'")
    assert [(f.text, f.col, f.gap_before) for f in line.fragments] == [
        ("TES", 1, None), ("AGING", None, 19)]


def test_doubled_quote_escape_survives():
    assert _segments("01 'IT''S HERE' +3 'OK'") == [("IT''S HERE", None), ("OK", 3)]


# ── line continuation: '+' is also the EZT continuation marker ───────────

def _payload(raw):
    return join_continuations(raw).strip().split(None, 1)[1]


def test_continuation_gap_with_space_before_plus():
    raw = "  TITLE 01 'TES CENTER' +19 +\n         'AGING REPORT'\n"
    assert _segments(_payload(raw)) == [("TES CENTER", None), ("AGING REPORT", 19)]


def test_continuation_gap_glued_to_literal():
    """'+19+' at end of line folds with no space -> "+19'AGING REPORT'"."""
    raw = "  TITLE 01 'TES CENTER' +19+\n         'AGING REPORT'\n"
    assert _segments(_payload(raw)) == [("TES CENTER", None), ("AGING REPORT", 19)]


def test_continuation_three_way_wrap():
    raw = "  TITLE 01 'TES' +5+\n     'CENTER' +19+\n     'AGING REPORT'\n"
    assert _segments(_payload(raw)) == [
        ("TES", None), ("CENTER", 5), ("AGING REPORT", 19)]


def test_continuation_bare_plus_keeps_both_literals():
    """A bare '+' carries no gap; both literals must survive and abut."""
    raw = "  TITLE 01 'TEST CENTER' +\n         'AGING REPORT'\n"
    assert _segments(_payload(raw)) == [
        ("TEST CENTER", None), ("AGING REPORT", None)]


# ── generated layouts ────────────────────────────────────────────────────

def test_chain_layout_widths_and_order():
    ws = _ws("  TITLE 01 'TES CENTER' +19 'AGING REPORT'\n")
    assert _width(ws, "WS-R-TITLE-01") == _DEFAULT_PRINT_WIDTH
    assert _rendered(ws, "WS-R-TITLE-01").startswith(
        "TES CENTER" + " " * 19 + "AGING REPORT")


def test_zero_gap_chain_concatenates_flush_left():
    ws = _ws("  TITLE 01 'TEST CENTER' 'AGING REPORT'\n")
    rendered = _rendered(ws, "WS-R-TITLE-01")
    assert _first_col(rendered) == 1
    assert rendered.startswith("TEST CENTERAGING REPORT")
    assert _width(ws, "WS-R-TITLE-01") == _DEFAULT_PRINT_WIDTH


def test_every_layout_fills_the_page_width():
    ws = _ws("  TITLE 01 'A' +5 'B'\n"
             "  HEADING 01 COL 10 'ACCT' +19 'NAME'\n"
             "  FOOTING 'END'\n"
             "  PRINT CUSTNO CUSTNAME BALANCE\n")
    for name in ("WS-R-TITLE-01", "WS-R-HDG-01", "WS-R-FOOT", "WS-R-DTL"):
        assert _width(ws, name) == _DEFAULT_PRINT_WIDTH, name


# ── ambiguous trailing word (may be a field, may be a keyword) ───────────

def test_undeclared_trailing_word_is_ignored():
    """An unmarked bare word that names no field must not become a column."""
    plain = _rendered(_ws("  TITLE 01 'REPORT TITLE'\n"), "WS-R-TITLE-01")
    trailing = _rendered(_ws("  TITLE 01 'REPORT TITLE' SKIP\n"), "WS-R-TITLE-01")
    assert plain == trailing            # same layout, keyword contributes nothing
    assert "?" not in trailing          # no phantom runtime column
    assert _first_col(trailing) > 1     # still centered, not flipped flush-left


def test_declared_trailing_field_is_kept():
    ws = _ws("  TITLE 01 'CUST: ' CUSTNO\n")
    rendered = _rendered(ws, "WS-R-TITLE-01")
    assert rendered.startswith("CUST: ?????")      # CUSTNO is PIC 9(5)


def test_marked_unknown_field_is_still_trusted():
    """COL/+N are explicit enough to accept a name the preamble lacks."""
    ws = _ws("  TITLE 01 COL 10 'ACCT' +5 NOSUCHFLD\n")
    assert "?" in _rendered(ws, "WS-R-TITLE-01")


# ── MOVE-targets comment block ───────────────────────────────────────────

def test_move_targets_emitted_for_detail_line():
    ws = _ws("  PRINT CUSTNO CUSTNAME\n")
    assert "* WS-R-DTL MOVE-targets:" in ws
    assert "CUSTNO PIC 9(5) -> WS-DTL-CUSTNO" in ws


def test_move_targets_emitted_for_title_field():
    ws = _ws("  TITLE 01 'CUST: ' +2 CUSTNO\n")
    assert "MOVE-targets" in ws
    assert "-> WS-R-T01-CUSTNO" in ws


def test_move_targets_names_match_the_generated_subfields():
    ws = _ws("  PRINT CUSTNO CUSTNAME BALANCE\n")
    declared = {m.group(1) for line in _layout(ws, "WS-R-DTL")
                if (m := re.match(r"\s+05\s+(WS-\S+)", line))}
    advertised = {line.split("->")[-1].strip()
                  for line in ws.splitlines()
                  if line.lstrip().startswith("*") and "->" in line}
    assert advertised <= declared, advertised - declared


def test_move_targets_never_truncates_the_target():
    """The target is the one string the model must copy character-exact."""
    target = "WS-DTL-CUSTOMER-ACCOUNT-2"
    for src_len in (6, 33, 34, 50):
        out = _move_targets_comment("WS-R-DTL", [("A" * src_len, target)])
        assert target in " ".join(out), src_len


def test_move_targets_lines_fit_the_code_area():
    target = "WS-DTL-CUSTOMER-ACCOUNT-2"
    for src_len in (6, 33, 34, 50, 70):
        for line in _move_targets_comment("WS-R-DTL", [("A" * src_len, target)]):
            assert len(line) <= 72, (src_len, len(line))


def test_move_targets_lines_are_comments_at_column_7():
    for line in _move_targets_comment("WS-R-DTL", [("CUSTNO", "WS-DTL-CUSTNO")]):
        assert line[6] == "*", repr(line)


# ── assembler interaction ────────────────────────────────────────────────

def test_dedupe_keeps_comment_of_surviving_block():
    """A discarded duplicate must not take the NEXT block's comment with it."""
    ws = "\n".join([
        "       01  WS-DUP.", "           05  A  PIC X(1).",
        "       01  WS-DUP.", "           05  B  PIC X(1).",
        "      * WS-R-DTL MOVE-targets:",
        "      *   CUSTNO PIC 9(5) -> WS-DTL-CUSTNO",
        "       01  WS-R-DTL.", "           05  WS-DTL-CUSTNO  PIC X(5).",
    ])
    out = _dedupe_ws_items(ws)
    assert "MOVE-targets" in out
    assert out.count("01  WS-DUP") == 1


def test_assembled_program_respects_column_72():
    """An over-long comment would fold onto a line that is no longer one."""
    src = PREAMBLE_SRC + """
FILE RPTFILE DISK 133

JOB INPUT CUSTFILE OUTPUT RPTFILE
  DISPLAY CUSTNO
END-JOB

REPORT CUSTRPT
  TITLE 01 'TES CENTER' +19 'AGING REPORT'
  PRINT CUSTNO CUSTNAME BALANCE STATUS
END-REPORT
"""
    sections = parse_ezt(src)
    pre = parse_preamble(src)
    converted = {}
    for s in sections:
        key = f"{s.type.value}:{s.name}"
        if s.type == SectionType.FILE_DEF:
            converted[key] = convert_file_def(src)
        elif s.type == SectionType.FIELD_DEF:
            converted[key] = convert_field_def(s.content)
        elif s.type == SectionType.REPORT:
            converted[f"report_ws:{s.name}"] = gen_report_ws(
                s.name, s.content, preamble=pre)
    converted["logic:combined"] = (
        "--- WORKING-STORAGE ---\n--- PROCEDURE ---\n"
        "       MAIN-PROCESS.\n           STOP RUN.\n"
    )
    cobol = assemble(sections, converted, program_name="T", source=src)
    for line in cobol.splitlines():
        assert len(line) <= 72, repr(line)
        if line.lstrip().startswith("*"):
            assert line[6] == "*", repr(line)


# ── standalone runner (no pytest required) ───────────────────────────────

if __name__ == "__main__":
    import traceback

    cases = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failures = 0
    for name, fn in cases:
        try:
            fn()
            print(f"  pass  {name}")
        except Exception:
            failures += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
    print(f"\n{len(cases) - failures}/{len(cases)} passed")
    sys.exit(1 if failures else 0)
