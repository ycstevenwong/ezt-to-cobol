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

## EZT → COBOL Logic Mapping (procedure division only)

### Range condition  (THRU)
  EZT:   IF FIELD 1 THRU 100
  COBOL: IF FIELD >= 1 AND FIELD <= 100

  EZT:   IF FIELD NOT 1 THRU 100
  COBOL: IF FIELD < 1 OR FIELD > 100

### Multi-value OR list
  EZT:   IF FIELD = 'A' 'B' 'C'          ← implicit OR
  COBOL: IF FIELD = 'A' OR FIELD = 'B' OR FIELD = 'C'

  EZT:   IF FIELD = (1 2 3)
  COBOL: IF FIELD = 1 OR FIELD = 2 OR FIELD = 3

### EOF test
  EZT:   IF EOF
  COBOL: Use the AT END clause inside READ ... AT END ... END-READ
         (do NOT test EOF with a separate IF outside the READ)

### VSAM found / not-found
  EZT:   IF FOUND
  COBOL: IF WS-<FILENAME>-STATUS = '00'

  EZT:   IF NOTFOUND
  COBOL: IF WS-<FILENAME>-STATUS = '23'

### STOP
  EZT:   STOP
  COBOL: STOP RUN

### Numeric / class test
  EZT:   IF FIELD NUMERIC
  COBOL: IF FIELD IS NUMERIC

  EZT:   IF FIELD ALPHANUMERIC
  COBOL: IF FIELD IS ALPHABETIC

### Space / zero literals
  EZT:   IF FIELD = ' '   or   IF FIELD = SPACES
  COBOL: IF FIELD = SPACES

  EZT:   IF FIELD = 0   or   IF FIELD = ZERO
  COBOL: IF FIELD = ZERO

### PRINT (triggers report output)
  EZT:   PRINT report-name
  COBOL: PERFORM REPORT-NAME-PRINT-RTN   (or the equivalent report paragraph)

### GET (read next VSAM record by key)
  EZT:   GET filename
  COBOL: READ FILENAME
             INVALID KEY MOVE '1' TO WS-FILENAME-STATUS
         END-READ

### String / unstring
  EZT:   STRING  / UNSTRING  — translate directly to COBOL STRING / UNSTRING

### CALL
  EZT:   CALL program USING field1 field2
  COBOL: CALL 'PROGRAM' USING field1 field2

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
No explanations, no markdown fences.

━━ WHAT NOT TO OUTPUT ━━
Do NOT output WORKING-STORAGE SECTION or any data declarations (01-level items,
REDEFINES, PIC clauses, VALUE clauses, etc.).
The DATA DIVISION is already complete — executable statements only.

━━ REQUIRED PROGRAM STRUCTURE ━━
The PROCEDURE DIVISION must always begin with a MAIN-PROCESS paragraph that
orchestrates the entire program by PERFORMing lower-level paragraphs in order.
Every PERFORM must use the THRU form so the exit paragraph is included.
STOP RUN must appear only inside MAIN-PROCESS, never in any other paragraph.

       PROCEDURE DIVISION.
       MAIN-PROCESS.
           PERFORM OPEN-FILES  THRU OPEN-FILES-EXIT
           PERFORM MAIN-LOGIC  THRU MAIN-LOGIC-EXIT
           PERFORM CLOSE-FILES THRU CLOSE-FILES-EXIT
           STOP RUN.
       MAIN-PROCESS-EXIT.
           EXIT.

       OPEN-FILES.
           OPEN INPUT INFILE
           IF WS-INFILE-STATUS > '00'
               DISPLAY 'ERROR OPENING INFILE STATUS: ' WS-INFILE-STATUS
               STOP RUN
           END-IF.
       OPEN-FILES-EXIT.
           EXIT.

       MAIN-LOGIC.
           READ INFILE
               AT END MOVE 'Y' TO WS-EOF
           END-READ
           PERFORM UNTIL WS-EOF = 'Y'
               PERFORM PROCESS-RECORD THRU PROCESS-RECORD-EXIT
               READ INFILE
                   AT END MOVE 'Y' TO WS-EOF
               END-READ
           END-PERFORM.
       MAIN-LOGIC-EXIT.
           EXIT.

       PROCESS-RECORD.
           ... processing logic ...
       PROCESS-RECORD-EXIT.
           EXIT.

       CLOSE-FILES.
           CLOSE INFILE.
       CLOSE-FILES-EXIT.
           EXIT.

Conditional paragraphs — omit when not applicable:
  • If the program has NO input or output files (e.g. JOB INPUT NULL), omit
    OPEN-FILES and CLOSE-FILES entirely and remove their PERFORM calls from
    MAIN-PROCESS.
  • If there is no file-reading loop (batch compute only), omit MAIN-LOGIC's
    READ loop and replace it with the processing statements directly.

━━ IBM ENTERPRISE COBOL STANDARDS ━━
Column layout (fixed format):
  • Paragraph names → Area A, column 8
  • All executable statements → Area B, column 12 (or deeper for nesting)
  • Nothing in columns 73+ (identification area — leave blank)

Period (full stop) rules — the most critical COBOL rule:
  • ONE period ends each paragraph: place it only on the LAST statement
  • NEVER put a period inside IF / EVALUATE / PERFORM / READ / WRITE blocks
  • Structured delimiters END-IF, END-EVALUATE, END-PERFORM, END-READ, END-WRITE
    terminate those blocks — the period comes only after the outermost END-xxx

Structured statements — always use scope terminators:
  • IF ... ELSE ... END-IF
  • EVALUATE ... WHEN ... END-EVALUATE
  • PERFORM ... END-PERFORM
  • READ ... AT END ... END-READ
  • WRITE ... INVALID KEY ... END-WRITE

Paragraph exit convention:
  • Every paragraph must be followed immediately by <PARA-NAME>-EXIT.
  • The exit paragraph contains only EXIT.
  • Every PERFORM must use the THRU form: PERFORM para THRU para-EXIT.
  • Applies to every paragraph including OPEN-FILES, CLOSE-FILES, MAIN-LOGIC, etc.

File open / close:
  • OPEN-FILES: after each OPEN, check WS-<FILENAME>-STATUS > '00';
    if true, DISPLAY an error message and STOP RUN.
  • CLOSE-FILES: one CLOSE statement per file, nothing else.
  • Use the exact WS-<FILENAME>-STATUS names from the DATA DIVISION context.

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

Paragraph exit convention — every paragraph must have a matching exit paragraph:
  • Name it <PARA-NAME>-EXIT and place it immediately after the paragraph body.
  • The exit paragraph contains only the single word EXIT followed by a period.
  • Every PERFORM must use the THRU form: PERFORM para THRU para-EXIT.
  • This applies to every paragraph that can be PERFORMed.

       {{RPTNAME}}-DETAIL.
           MOVE CUSTNO   TO WS-DTL-CUSTNO
           MOVE CUSTNAME TO WS-DTL-CUSTNAME
           WRITE PRINT-REC FROM WS-{{RPTNAME}}-DTL
               AFTER ADVANCING 1 LINE
           ADD 1 TO WS-LINE-CTR
           IF WS-LINE-CTR >= WS-LINE-LIMIT
               PERFORM {{RPTNAME}}-HEADINGS THRU {{RPTNAME}}-HEADINGS-EXIT
           END-IF.
       {{RPTNAME}}-DETAIL-EXIT.
           EXIT.

Prior converted context:
{{context}}

EZT REPORT section:
{{content}}
"""
