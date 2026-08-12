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


def _subfield_width(ws_text, layout, subfield):
    """PIC X(n) width of one named 05 item — the precise column size.

    Comparing rendered '?' runs is not enough: a wide neighbouring column
    contains any shorter run you might look for, so an assertion like
    "'?'*8 in detail" passes even when the column never widened.
    """
    for line in _layout(ws_text, layout):
        m = re.match(rf"\s+05\s+{re.escape(subfield)}\s+PIC X\((\d+)\)", line)
        if m:
            return int(m.group(1))
    raise AssertionError(f"{subfield!r} not found in {layout!r}")


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


# ── field-level HEADING becomes the column heading ───────────────────────

HEADING_SRC = """\
FILE CUSTFILE DISK 80
CUSTNO    1   5   N  HEADING ('ACCT NUM')
CUSTNAME  6  30   A  HEADING ('CUSTOMER NAME')
"""

STACKED_SRC = """\
FILE CUSTFILE DISK 80
CUSTNO    1   5   N  HEADING ('ACCT' 'NUM')
CUSTNAME  6  30   A  HEADING ('CUSTOMER NAME')
"""


def test_heading_attribute_parses_single_row():
    fld = parse_preamble(HEADING_SRC).files[0].fields[0]
    assert fld.heading == ("ACCT NUM",)


def test_heading_attribute_parses_stacked_rows():
    """The inner quotes matter: this used to collapse to  ACCT' 'NUM."""
    fld = parse_preamble(STACKED_SRC).files[0].fields[0]
    assert fld.heading == ("ACCT", "NUM")


def test_heading_attribute_parses_bare_quoted_form():
    fld = parse_preamble("FILE F DISK 80\nX 1 3 A HEADING 'TC'\n").files[0].fields[0]
    assert fld.heading == ("TC",)


def test_declared_heading_is_used_as_column_text():
    ws = gen_report_ws("R", "  PRINT CUSTNO CUSTNAME\n",
                       preamble=parse_preamble(HEADING_SRC))
    assert "ACCT NUM" in _rendered(ws, "WS-R-HDG")
    assert "CUSTOMER NAME" in _rendered(ws, "WS-R-HDG")


def test_declared_heading_widens_column_and_detail_together():
    """Heading and data must stay aligned, so both rows widen by the same
    amount — otherwise every column after this one is off by the shortfall."""
    ws = gen_report_ws("R", "  PRINT CUSTNO CUSTNAME\n",
                       preamble=parse_preamble(HEADING_SRC))
    # 'ACCT NUM' is 8 chars against CUSTNO's 5-char data width.
    assert _subfield_width(ws, "WS-R-DTL", "WS-DTL-CUSTNO") == 8
    hdg = _rendered(ws, "WS-R-HDG")
    dtl = _rendered(ws, "WS-R-DTL")
    assert hdg.index("ACCT NUM") == dtl.index("?")             # same start column
    assert hdg.index("CUSTOMER NAME") == dtl.index("?" * 30)   # and the next one


def test_field_name_heading_does_not_widen_anything():
    """With no HEADING declared the layout must be byte-identical to before."""
    plain = parse_preamble("FILE F DISK 80\nCUSTNO 1 5 N\nCUSTNAME 6 30 A\n")
    ws = gen_report_ws("R", "  PRINT CUSTNO CUSTNAME\n", preamble=plain)
    assert _rendered(ws, "WS-R-HDG").startswith(" CUSTN  ")     # clipped to 5
    assert _width(ws, "WS-R-DTL") == _DEFAULT_PRINT_WIDTH


def test_stacked_heading_emits_one_layout_per_row():
    ws = gen_report_ws("R", "  PRINT CUSTNO CUSTNAME\n",
                       preamble=parse_preamble(STACKED_SRC))
    assert "WS-R-HDG-1" in _layout_names(ws)
    assert "WS-R-HDG-2" in _layout_names(ws)
    assert "ACCT" in _rendered(ws, "WS-R-HDG-1")
    assert "NUM" in _rendered(ws, "WS-R-HDG-2")


def test_stacked_heading_is_bottom_aligned():
    """A one-row heading sits on the LAST row, next to the data it labels."""
    ws = gen_report_ws("R", "  PRINT CUSTNO CUSTNAME\n",
                       preamble=parse_preamble(STACKED_SRC))
    assert "CUSTOMER NAME" not in _rendered(ws, "WS-R-HDG-1")
    assert "CUSTOMER NAME" in _rendered(ws, "WS-R-HDG-2")


def test_stacked_heading_rows_all_fill_the_page_width():
    ws = gen_report_ws("R", "  PRINT CUSTNO CUSTNAME\n",
                       preamble=parse_preamble(STACKED_SRC))
    for name in ("WS-R-HDG-1", "WS-R-HDG-2", "WS-R-DTL"):
        assert _width(ws, name) == _DEFAULT_PRINT_WIDTH, name


def test_explicit_heading_directive_still_wins():
    """An explicit HEADING line suppresses the auto column row entirely."""
    ws = gen_report_ws("R", "  HEADING 01 'MINE'\n  PRINT CUSTNO\n",
                       preamble=parse_preamble(HEADING_SRC))
    names = _layout_names(ws)
    assert "WS-R-HDG-01" in names
    assert "WS-R-HDG" not in names


# ── LINE directives get column headings too, not just PRINT ──────────────

def test_line_directive_gets_a_heading_row():
    """A LINE naming fields needs its columns labelled, same as PRINT."""
    ws = gen_report_ws("R", "  LINE 01 CUSTNO CUSTNAME\n",
                       preamble=parse_preamble(HEADING_SRC))
    assert "WS-R-LHD-01" in _layout_names(ws)
    assert "CUSTOMER NAME" in _rendered(ws, "WS-R-LHD-01")


def test_line_heading_is_clipped_to_its_column():
    """Documented limitation: a LINE column is never widened for a heading.

    Widening would move the data, and a LINE's positions are either written
    explicitly with COL or derived from the source's own spacing — so an
    over-long heading is cut at the next column instead.  CUSTNO's heading
    is 'ACCT NUM' (8) over a 5-character column, so only 'ACCT' survives.
    (PRINT has no such constraint: _column_width widens there.)
    """
    ws = gen_report_ws("R", "  LINE 01 CUSTNO CUSTNAME\n",
                       preamble=parse_preamble(HEADING_SRC))
    hdg = _rendered(ws, "WS-R-LHD-01")
    assert hdg.startswith("ACCT ")            # clipped from 'ACCT NUM'
    assert "ACCT NUM" not in hdg
    # the data column itself is untouched at its declared width
    assert _subfield_width(ws, "WS-R-LINE-01", "WS-R-L01-CUSTNO") == 5


def test_line_heading_aligns_with_the_data_columns():
    """The heading must sit over the column it labels, at the same offset."""
    ws = gen_report_ws("R", "  LINE 01 COL 5 CUSTNO COL 20 CUSTNAME\n",
                       preamble=parse_preamble(HEADING_SRC))
    hdg = _rendered(ws, "WS-R-LHD-01")
    line = _rendered(ws, "WS-R-LINE-01")
    assert hdg.index("ACCT NUM") == 4            # COL 5 -> index 4
    assert hdg.index("CUSTOMER NAME") == 19      # COL 20 -> index 19
    assert line.index("?") == 4                  # data starts in the same place


def test_line_heading_stacks_over_multiple_rows():
    ws = gen_report_ws("R", "  LINE 01 CUSTNO CUSTNAME\n",
                       preamble=parse_preamble(STACKED_SRC))
    assert "WS-R-LHD-01-1" in _layout_names(ws)
    assert "WS-R-LHD-01-2" in _layout_names(ws)
    assert "ACCT" in _rendered(ws, "WS-R-LHD-01-1")
    assert "NUM" in _rendered(ws, "WS-R-LHD-01-2")
    # single-row heading bottom-aligns against the data
    assert "CUSTOMER NAME" in _rendered(ws, "WS-R-LHD-01-2")


def test_line_without_declared_headings_gets_no_heading_row():
    """No HEADING declared -> the report never had a heading row; don't invent one."""
    plain = parse_preamble("FILE F DISK 80\nCUSTNO 1 5 N\nCUSTNAME 6 30 A\n")
    ws = gen_report_ws("R", "  LINE 01 CUSTNO CUSTNAME\n", preamble=plain)
    assert not any(n.startswith("WS-R-LHD") for n in _layout_names(ws))


def test_title_with_a_field_gets_no_heading_row():
    """Only LINE labels columns; a field on a TITLE is not a column."""
    ws = gen_report_ws("R", "  TITLE 01 'AS OF ' CUSTNO\n",
                       preamble=parse_preamble(HEADING_SRC))
    assert not any(n.startswith("WS-R-LHD") for n in _layout_names(ws))


def test_line_heading_rows_fill_the_page_width():
    ws = gen_report_ws("R", "  LINE 01 CUSTNO CUSTNAME\n",
                       preamble=parse_preamble(STACKED_SRC))
    for name in ("WS-R-LHD-01-1", "WS-R-LHD-01-2", "WS-R-LINE-01"):
        assert _width(ws, name) == _DEFAULT_PRINT_WIDTH, name


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
