# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A CLI tool that converts Easytrieve Plus (EZT) mainframe report programs into
compilable IBM Enterprise COBOL. Structural constructs (FILE/FIELD/DEFINE
declarations, report layout scaffolding, file open/close error handling) are
converted deterministically in pure Python. Only the executable logic (JOB +
REPORT bodies) is sent to an LLM, which emits just the PROCEDURE DIVISION.
This split exists because structure is mechanical and must compile every
time, while logic translation benefits from an LLM's judgment — but the LLM
is never trusted with anything Python can generate deterministically.

## Commands

```bash
pip install -r requirements.txt

# Convert one file to stdout
python convert.py examples/sample.ezt

# Convert to a directory, verbose, against a local Ollama server (default)
python convert.py examples/sample.ezt -o output/ -v

# Point at a different OpenAI-compatible endpoint
python convert.py examples/sample.ezt --base-url https://api.openai.com/v1 --api-key $OPENAI_API_KEY --model gpt-4o

# Parse-only, no LLM call (sanity-check section splitting)
python convert.py examples/sample.ezt --dry-run
```

There is no test suite, linter, or build step configured in this repo.
`.claude/settings.local.json` shows the ad-hoc verification pattern used
during development — importing each module to check nothing is broken:

```bash
python -c "from src.converter import convert_all, convert_logic, COMBINED_LOGIC_KEY; from src.assembler import assemble; from src.prompts import SYSTEM_PROMPT, LOGIC_PROMPT; print('all imports ok')"
```

When changing `rule_converter.py` or `structured_parser.py`, verify against
`examples/sample.ezt` / `examples/sample_no_endings.ezt` and inspect the
`--- FILE-CONTROL ---` / `--- FILE-SECTION ---` / `--- WORKING-STORAGE ---`
marker blocks (see `docs/rule_converter_walkthrough.md`) or the assembled
output in `output/`.

## Pipeline architecture

```
convert.py (CLI)
   │
   ▼
src/parser.py            parse_ezt(source) → ordered list of EZTSection
   │                     (splits into FILE_DEF / FIELD_DEF / JOB / REPORT
   │                      by scanning for FILE/JOB/REPORT/END-* keywords)
   ▼
src/converter.py         convert_all(client, sections, source)
   │
   ├── FILE_DEF   → src/rule_converter.convert_file_def()      (deterministic)
   ├── FIELD_DEF  → src/rule_converter.convert_field_def()     (deterministic)
   ├── REPORT WS  → src/rule_converter.gen_report_ws()         (deterministic)
   ├── open/close → src/rule_converter.gen_open_close_paragraphs() (deterministic,
   │                driven by rules/copybooks.yaml hooks)
   └── JOB+REPORT → ONE combined LLM call (src/prompts.py) → PROCEDURE DIVISION
   ▼
src/assembler.py         assemble(sections, converted, program_name, source)
                          stitches every piece into one compilable .cbl,
                          then post-processes the LLM's procedure text.
```

Key point: **all JOB and REPORT sections are sent to the LLM in a single
combined call** (`convert_logic` in `src/converter.py`), not one call per
section. This is deliberate — the model sees the whole program at once and
emits one unified PROCEDURE DIVISION, avoiding duplicated paragraphs that
per-section calls used to produce. The LLM is given an explicit
"AVAILABLE WORKING-STORAGE + FILE-SECTION IDENTIFIERS" allow-list
(`_extract_var_names` in `src/converter.py`) so it references real
declarations instead of inventing them.

### Two pure-Python transform modules

`src/structured_parser.py` parses EZT preamble text (FILE/DEFINE lines) into
typed dataclasses (`EZTFile`, `EZTField`, `EZTDefine`, `Preamble`) — this is
the only module that reads raw EZT syntax for the preamble. `src/rule_converter.py`
consumes those dataclasses and emits formatted COBOL text; it never parses
EZT itself. Full walkthrough with worked example: `docs/rule_converter_walkthrough.md`.

Three public entry points into `rule_converter.py`, all called from
`converter.convert_all`:
- `convert_file_def(source)` → FILE-CONTROL + FILE SECTION + file-status WS
- `convert_field_def(content)` → WORKING-STORAGE items from DEFINEs
- `gen_report_ws(name, content, preamble)` → per-report counters, TITLE/HEADING/
  detail-line layouts, SUM/COUNT accumulators

VSAM files get a synthetic `<FILE>-KEY` field auto-injected
(`_inject_vsam_key`) since EZT VSAM declarations don't carry an explicit key
field but COBOL `RECORD KEY IS` needs one.

### Assembler post-processing (`src/assembler.py`)

The LLM's raw output is never trusted as final COBOL — `assemble()` runs it
through several deterministic passes, each documented inline at its
definition:
- `split_ws_proc` — splits the `--- WORKING-STORAGE ---` / `--- PROCEDURE ---`
  marker response into the two blocks.
- `_dedupe_ws_items` — drops duplicate 01-level WS declarations (rule-generated
  wins over LLM-generated).
- `_rename_reserved_paragraphs` — renames paragraphs like `INITIAL.` that
  collide with COBOL reserved words to `INITIAL-RTN.` (definitions and
  PERFORM references together).
- `_fix_integer_class_test` — rewrites the LLM's occasional `IS INTEGER`
  (not real COBOL) to `IS NUMERIC`.
- `add_statement_periods` / `_ensure_period_before_paragraphs` — normalizes
  COBOL's error-prone period placement (never inside IF/EVALUATE/PERFORM
  scopes, mandatory before the next paragraph header).
- `gen_missing_var_ws` — scans final procedure text for identifiers used but
  never declared, infers a PIC from usage context (arithmetic, MOVE partner,
  literal comparison, subscript), and auto-declares them under an
  `AUTO-DECLARED` comment flagged for human review.
- `_enforce_col_limit` / `_wrap_line` — wraps any line past COBOL's column-72
  limit using proper fixed-format continuation (statement continuation vs.
  literal continuation with `-` in column 7).

### YAML-driven rules (`rules/`)

- `rules/ezt_to_cobol.yaml` + `rules/report_scaffolding.yaml` — loaded by
  `src/rules.py` and formatted into the LLM system/user prompts
  (`src/prompts.py`). Editing these changes what the LLM is told, not what
  Python generates directly.
- `rules/copybooks.yaml` — declarative event→copybook hooks (see the
  extensive header comment in that file). Drives `gen_open_close_paragraphs`
  and the `COPY` lines the assembler emits. Only `file_open_failure` and
  `file_close_failure` are currently wired to actually-generated code
  (`_PY_GENERATED_EVENTS` in `src/assembler.py`); other events in the YAML
  (`vsam_invalid_key`, `sql_error`, `abend`) are declarative placeholders for
  future phases.

### Adding a new deterministic (non-LLM) conversion rule

Prefer extending `rule_converter.py`/`structured_parser.py` over prompt
engineering whenever the source EZT shape is fixed and mechanical (e.g. a new
FILE/DEFINE attribute) — it's what keeps output compilable. Reserve prompt
changes (`src/prompts.py`, `rules/ezt_to_cobol.yaml`) for judgment calls in
JOB/REPORT executable logic that don't have one deterministic COBOL shape.
