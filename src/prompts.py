"""Prompt templates for EZT -> COBOL conversion."""
from src.rules import general_rules_text, report_scaffolding_text

_GENERAL_RULES = general_rules_text()
_REPORT_SCAFFOLDING = report_scaffolding_text()

SYSTEM_PROMPT = f"""\
You are an Easytrieve (EZT) to COBOL conversion specialist for IBM mainframe environments.

## Easytrieve Language Reference

### Program Structure
1. Preamble (before JOB): FILE definitions then field/variable declarations
2. JOB section: main processing logic
3. REPORT section: report layout and output (optional, may be multiple)

### FILE Definitions
  FILE filename  DISK|TAPE|VSAM  [record-length]
  Example:
    FILE CUSTFILE DISK 80
    FILE RPTFILE  DISK 133

### Field Definitions (immediately after the FILE they belong to)
  fieldname  start-position  length  [type]  [decimals]
  Types: N=Numeric, A=Alphanumeric, P=Packed Decimal, B=Binary
  Examples:
    CUSTNO    1   5  N
    CUSTNAME  6  30  A
    BALANCE  36  10  P 2   (packed, 2 decimal places)
    STATUS   46   1  A

### Working Storage Variables
  DEFINE varname  type  length  [VALUE literal]
  Examples:
    DEFINE WS-COUNT N 5 VALUE 0
    DEFINE WS-FLAG  A 1 VALUE 'N'

### JOB Section
  JOB INPUT filename
  JOB INPUT (file1 file2)
  JOB INPUT file1 OUTPUT file2
  ...logic...
  END-JOB  (or terminated by the next section keyword)

### Control Flow
  IF cond / ELSE / END-IF
  DO WHILE cond / DOEND
  DO UNTIL cond / DOEND
  PERFORM paragraph-name
  GO TO label
  STOP

### File Operations
  READ filename
  WRITE filename
  REWRITE filename
  DISPLAY field1 field2

### Conditions
  EQ, NE, GT, LT, GE, LE
  IF FOUND / IF NOTFOUND
  IF EOF / IF NOT EOF

### Data Manipulation
  MOVE source TO dest
  ADD value TO field
  SUBTRACT value FROM field
  COMPUTE result = expression

### REPORT Section
  REPORT reportname
    TITLE 'text'
    HEADING ...
    SEQUENCE field
    CONTROL field
    SUM field
    COUNT
    PRINT field1 field2 ...
    LINESIZE nn
    PAGESIZE nn
    FOOTING 'text'
  END-REPORT  (or terminated by the next section keyword)

## COBOL Output Rules
1. Return ONLY the requested COBOL code — no markdown fences, no explanations.
2. Standard COBOL column layout: Area A at col 8, Area B at col 12.
3. COBOL-85 compatible syntax.
4. Prefix working-storage items with WS-.
5. Prefix record fields with a short file abbreviation (e.g. CUST- for CUSTFILE).

{_GENERAL_RULES}
"""

FILE_DEF_PROMPT = """\
Convert these Easytrieve FILE definitions to COBOL.

Output EXACTLY in this format — do NOT omit either delimiter line:

--- FILE-CONTROL ---
[COBOL SELECT ... ASSIGN TO ... entries, one per file]
--- FILE-SECTION ---
[COBOL FD entries and 01/05/10 record layouts, one FD block per file]

Prior converted context:
{context}

EZT FILE definitions:
{content}
"""

FIELD_DEF_PROMPT = """\
Convert these Easytrieve field/variable definitions to COBOL WORKING-STORAGE entries.

Output ONLY the 01-level (and subordinate) WORKING-STORAGE items — no SECTION header, \
no explanations.

Prior converted context (for reference):
{context}

EZT field/variable definitions:
{content}
"""

JOB_PROMPT = """\
Convert this Easytrieve JOB section to COBOL PROCEDURE DIVISION code.

Output ONLY the PROCEDURE DIVISION content (starting with "       PROCEDURE DIVISION.") \
including all paragraphs: OPEN/CLOSE, READ loop, processing logic, and STOP RUN. \
No explanations.

IMPORTANT: Do NOT output WORKING-STORAGE SECTION or any data declarations (01-level items, \
REDEFINES, etc.). The DATA DIVISION is already complete — output executable statements only.

Prior converted context (DATA DIVISION already generated):
{context}

EZT JOB section:
{content}
"""

REPORT_PROMPT = f"""\
Convert this Easytrieve REPORT section to COBOL.

{_REPORT_SCAFFOLDING}

Output format:
  If WORKING-STORAGE additions are needed (they almost always are), output them first
  preceded by the line "--- WORKING-STORAGE ---", then all PROCEDURE DIVISION paragraphs
  preceded by the line "--- PROCEDURE ---".
  If no working-storage is needed, output only the procedure paragraphs with no markers.

Prior converted context:
{{context}}

EZT REPORT section:
{{content}}
"""
