"""Prompt templates for EZT -> COBOL conversion."""
from src.rules import general_rules_text, report_scaffolding_text

_GENERAL_RULES = general_rules_text()
_REPORT_SCAFFOLDING = report_scaffolding_text()

SYSTEM_PROMPT = f"""\
You are an Easytrieve (EZT) to COBOL conversion specialist for IBM mainframe environments.

## Easytrieve Language Reference

### Program Structure
1. Preamble (before JOB): FILE definitions then field/variable declarations
   (FILE/FIELD sections are converted separately — you will receive context
   showing the DATA DIVISION already generated from them)
2. JOB section: main processing logic
3. REPORT section: report layout and output (optional, may be multiple)

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

JOB_PROMPT = """\
Convert this Easytrieve JOB section to IBM Enterprise COBOL PROCEDURE DIVISION code.

━━ WHAT TO OUTPUT ━━
Output ONLY the PROCEDURE DIVISION content, starting with:
       PROCEDURE DIVISION.
Include all paragraphs: OPEN/CLOSE, main READ loop, processing logic, STOP RUN.
No explanations, no markdown fences.

━━ WHAT NOT TO OUTPUT ━━
Do NOT output WORKING-STORAGE SECTION or any data declarations (01-level items,
REDEFINES, PIC clauses, VALUE clauses, etc.).
The DATA DIVISION is already complete — executable statements only.

━━ IBM ENTERPRISE COBOL STANDARDS ━━
Column layout (fixed format):
  • Paragraph names and division/section headers → Area A, column 8
  • All executable statements → Area B, column 12 (or deeper for nesting)
  • Nothing in columns 73+ (identification area — leave blank)

Period (full stop) rules — the most critical COBOL rule:
  • ONE period ends each paragraph: place it only on the LAST statement of the paragraph
  • NEVER put a period inside IF / EVALUATE / PERFORM / READ / WRITE blocks
  • Structured delimiters END-IF, END-EVALUATE, END-PERFORM, END-READ, END-WRITE
    terminate those blocks — the period comes only after the outermost END-xxx

Structured statements — always use scope terminators:
  • IF ... ELSE ... END-IF          (no period inside)
  • EVALUATE ... WHEN ... END-EVALUATE
  • PERFORM ... END-PERFORM         (inline PERFORM must have END-PERFORM)
  • READ ... AT END ... END-READ
  • WRITE ... INVALID KEY ... END-WRITE

Correct paragraph structure example:
       PROCESS-RECORD.
           IF WS-STATUS = 'A'
               PERFORM WRITE-OUTPUT
               ADD 1 TO WS-COUNTER
           ELSE
               ADD 1 TO WS-SKIP-CTR
           END-IF
           READ INPUT-FILE
               AT END MOVE 'Y' TO WS-EOF
           END-READ.

Prior converted context (DATA DIVISION already generated):
{context}

EZT JOB section:
{content}
"""

REPORT_PROMPT = f"""\
Convert this Easytrieve REPORT section to IBM Enterprise COBOL PROCEDURE DIVISION code.

{_REPORT_SCAFFOLDING}

━━ WHAT TO OUTPUT ━━
Output ONLY the PROCEDURE DIVISION paragraphs — headings, detail print,
control-break logic, end-of-report.
No explanations, no markdown fences.

━━ WHAT NOT TO OUTPUT ━━
Do NOT output WORKING-STORAGE SECTION or any data declarations (01-level items,
REDEFINES, PIC clauses, VALUE clauses, etc.).
The DATA DIVISION is already complete — Python has already generated:
  WS-PAGE-CTR, WS-LINE-CTR, WS-PAGE-LIMIT, WS-LINE-LIMIT, PRINT-REC
  WS-{{FIELD}}-TOT, WS-{{FIELD}}-TOT-D  (for each SUM field)
  WS-{{RPTNAME}}-CNT, WS-{{RPTNAME}}-CNT-D  (if COUNT present)

━━ IBM ENTERPRISE COBOL STANDARDS ━━
Column layout (fixed format):
  • Paragraph names and division/section headers → Area A, column 8
  • All executable statements → Area B, column 12 (or deeper for nesting)
  • Nothing in columns 73+ (identification area — leave blank)

Period (full stop) rules — the most critical COBOL rule:
  • ONE period ends each paragraph: place it only on the LAST statement of the paragraph
  • NEVER put a period inside IF / EVALUATE / PERFORM / READ / WRITE blocks
  • Structured delimiters END-IF, END-EVALUATE, END-PERFORM, END-READ, END-WRITE
    terminate those blocks — the period comes only after the outermost END-xxx

Structured statements — always use scope terminators:
  • IF ... ELSE ... END-IF          (no period inside)
  • EVALUATE ... WHEN ... END-EVALUATE
  • PERFORM ... END-PERFORM         (inline PERFORM must have END-PERFORM)
  • READ ... AT END ... END-READ
  • WRITE ... INVALID KEY ... END-WRITE

Correct paragraph structure example:
       {{RPTNAME}}-DETAIL.
           MOVE CUSTNO   TO WS-DTL-CUSTNO
           MOVE CUSTNAME TO WS-DTL-CUSTNAME
           WRITE PRINT-REC FROM WS-{{RPTNAME}}-DTL
               AFTER ADVANCING 1 LINE
           ADD 1 TO WS-LINE-CTR
           IF WS-LINE-CTR >= WS-LINE-LIMIT
               PERFORM {{RPTNAME}}-HEADINGS
           END-IF.

Prior converted context:
{{context}}

EZT REPORT section:
{{content}}
"""
