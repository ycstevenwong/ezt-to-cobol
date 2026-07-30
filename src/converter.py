"""Orchestrate EZT-to-COBOL conversion: rule-based for structure, AI for logic."""
import requests as _requests
from typing import Dict, List

import re
from src.assembler import split_ws_proc
from src.parser import EZTSection, SectionType
from src.prompts import SYSTEM_PROMPT, LOGIC_PROMPT
from src.rule_converter import (
    convert_file_def,
    convert_field_def,
    gen_open_close_paragraphs,
    gen_report_ws,
    parse_job_file_modes,
    _inject_vsam_key,
)
from src.rules import load_copybooks
from src.structured_parser import parse_preamble

DEFAULT_MODEL    = "llama3.2"
DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_API_KEY  = "ollama"
DEFAULT_TIMEOUT  = 60
MAX_TOKENS       = 8192

_RULE_BASED = {SectionType.FILE_DEF, SectionType.FIELD_DEF}

# Synthetic key for the single combined JOB+REPORT LLM result in the
# converted-output dict.  The assembler looks for this key after iterating
# the rule-based sections.
COMBINED_LOGIC_KEY = "logic:combined"

# Synthetic key for the Python-generated OPEN-FILES / CLOSE-FILES paragraphs.
# The assembler appends this text to procedure_parts and skips the LLM's
# version (the prompt tells the LLM these are pre-generated).
OPEN_CLOSE_KEY = "open_close:paragraphs"


def _report_ws_key(report_name: str) -> str:
    """Key under which the rule-converter-generated per-report WS lives in
    the converted-output dict.  `assemble` reads from here so the layouts
    aren't regenerated.
    """
    return f"report_ws:{report_name}"


def _section_key(section: EZTSection) -> str:
    return f"{section.type.value}:{section.name}"


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"


_VAR_LINE_RE = re.compile(
    r"^\s+(?:0[1-9]|[1-4][0-9])\s+([A-Z][A-Z0-9-]*)\b", re.IGNORECASE
)


def _extract_var_names(context: str) -> List[str]:
    """Pull every named data-item from the DATA DIVISION context.

    Matches lines of the form ``<level> <NAME>`` (any COBOL level 01-49)
    and drops FILLER entries.  Used to give the LLM an explicit allow-list
    so it doesn't invent variables that don't exist in WORKING-STORAGE.
    """
    seen: dict = {}
    for line in context.splitlines():
        m = _VAR_LINE_RE.match(line)
        if not m:
            continue
        name = m.group(1).upper()
        if name == "FILLER":
            continue
        seen[name] = True
    return sorted(seen)


def make_client(
    base_url:   str  = DEFAULT_BASE_URL,
    api_key:    str  = DEFAULT_API_KEY,
    verify_ssl: bool = True,
) -> dict:
    """Return a connection-config dict for the LLM REST endpoint.

    The returned dict is passed to convert_logic / convert_all.
    base_url should be the OpenAI-compatible root, e.g.
      http://localhost:11434/v1   (Ollama)
      https://api.openai.com/v1  (OpenAI)
      https://your-internal-llm/v1
    """
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url = f"{url}/chat/completions"
    return {
        "url":     url,
        "headers": {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        "verify":  verify_ssl,
    }


def convert_logic(
    client:   dict,
    sections: List[EZTSection],
    context:  str,
    model:    str  = DEFAULT_MODEL,
    verbose:  bool = False,
) -> str:
    """POST the combined JOB + REPORT logic to the LLM in a single call.

    The LLM sees every executable section at once and emits one unified
    PROCEDURE DIVISION, which avoids the previous duplication where REPORT
    re-emitted paragraphs it had seen in JOB's prior output.
    """
    content_blocks = []
    for s in sections:
        content_blocks.append(
            f"--- EZT {s.type.value.upper()} ({s.name}) ---\n{s.content}"
        )
    combined_content = "\n\n".join(content_blocks)

    # Build an explicit allow-list of every named data-item that already
    # exists in the DATA DIVISION context.  The LLM is told it MUST pick
    # variable references from this list (or declare new ones in its WS
    # block) — without it, the LLM tends to invent identifiers that don't
    # compile because they were never declared.
    available_vars = _extract_var_names(context)
    if available_vars:
        var_listing = (
            "AVAILABLE WORKING-STORAGE + FILE-SECTION IDENTIFIERS\n"
            "(reference ONLY these; if you need something not listed, add\n"
            " it to your --- WORKING-STORAGE --- block):\n"
            + "\n".join(f"  - {n}" for n in available_vars)
        )
        full_context = f"{context}\n\n{var_listing}" if context else var_listing
    else:
        full_context = context or "(none yet — no rule-based sections)"

    user_message = LOGIC_PROMPT.format_map(_SafeDict(
        context=full_context,
        content=combined_content,
    ))

    if verbose:
        names = ", ".join(f"{s.type.value}:{s.name}" for s in sections)
        print(f"  → [logic] combined call for {names}", flush=True)

    body = {
        "model":      model,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
        "stream": False,
    }

    resp = _requests.post(
        client["url"],
        headers=client["headers"],
        json=body,
        timeout=DEFAULT_TIMEOUT,
        verify=client.get("verify", True),
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def convert_all(
    client:   dict,
    sections: List[EZTSection],
    source:   str,
    model:    str  = DEFAULT_MODEL,
    verbose:  bool = False,
) -> Dict[str, str]:
    """Convert every EZT section.

    FILE_DEF and FIELD_DEF are converted deterministically via rule_converter.
    All JOB and REPORT sections are converted together in a single LLM call
    so the model sees the full program at once and emits one unified
    PROCEDURE DIVISION with no duplicated paragraphs.
    """
    results: Dict[str, str] = {}
    context_chunks: List[str] = []

    # 1. Rule-based sections (FILE_DEF + FIELD_DEF) first, in order.
    for section in sections:
        if section.type not in _RULE_BASED:
            continue
        if section.type == SectionType.FILE_DEF:
            cobol = convert_file_def(source)
        else:
            cobol = convert_field_def(section.content)
        if verbose:
            print(f"  → [{section.type.value}] {section.name} (rule-based)", flush=True)
        results[_section_key(section)] = cobol
        context_chunks.append(
            f"=== {section.type.value.upper()} ({section.name})"
            f" — ALREADY IN DATA DIVISION, DO NOT REDECLARE ===\n{cobol}"
        )

    # 2. Per-report WS layouts (counters, accumulators, TITLE/HDG/DTL/etc.).
    #    Generated here — BEFORE the LLM call — so the layout names appear
    #    in the context the LLM sees, and so the LLM's procedure code can
    #    reference them without inventing its own.
    preamble = parse_preamble(source) if source else None

    # 2a. OPEN-FILES / CLOSE-FILES paragraphs — Python-generated so each
    #     file gets a consistent status-check that PERFORMs the configured
    #     copybook paragraph (rules/copybooks.yaml).  Stashed under
    #     OPEN_CLOSE_KEY for the assembler; the LLM is instructed (via the
    #     prompt) NOT to emit these paragraphs itself.
    hooks = load_copybooks()
    if preamble and preamble.files:
        files = [_inject_vsam_key(f) for f in preamble.files]
        job_section = next(
            (s for s in sections if s.type == SectionType.JOB), None,
        )
        file_modes = (
            parse_job_file_modes(job_section.content) if job_section else {}
        )
        open_close = gen_open_close_paragraphs(files, file_modes, hooks)
        if open_close:
            if verbose:
                print("  → [open-close] (rule-based)", flush=True)
            results[OPEN_CLOSE_KEY] = open_close
            context_chunks.append(
                "=== OPEN-FILES / CLOSE-FILES — ALREADY GENERATED, "
                "DO NOT REDECLARE ===\n" + open_close
            )

    for section in sections:
        if section.type != SectionType.REPORT:
            continue
        py_ws = gen_report_ws(section.name, section.content, preamble=preamble)
        if not py_ws:
            continue
        if verbose:
            print(f"  → [report-ws] {section.name} (rule-based)", flush=True)
        results[_report_ws_key(section.name)] = py_ws
        context_chunks.append(
            f"=== REPORT-WS ({section.name})"
            f" — ALREADY IN DATA DIVISION, DO NOT REDECLARE ===\n{py_ws}"
        )

    # 3. Single combined LLM call for every JOB + REPORT section.
    logic_sections = [s for s in sections if s.type not in _RULE_BASED]
    if logic_sections:
        context = "\n\n".join(context_chunks)
        results[COMBINED_LOGIC_KEY] = convert_logic(
            client, logic_sections, context, model=model, verbose=verbose
        )

    return results
