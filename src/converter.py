"""Orchestrate EZT-to-COBOL conversion: rule-based for structure, AI for logic."""
import requests as _requests
from typing import Dict, List

from src.assembler import split_ws_proc
from src.parser import EZTSection, SectionType
from src.prompts import SYSTEM_PROMPT, JOB_PROMPT, REPORT_PROMPT
from src.rule_converter import convert_file_def, convert_field_def

DEFAULT_MODEL    = "llama3.2"
DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_API_KEY  = "ollama"
DEFAULT_TIMEOUT  = 60
MAX_TOKENS       = 8192

_RULE_BASED = {SectionType.FILE_DEF, SectionType.FIELD_DEF}

_PROMPT_FOR_TYPE = {
    SectionType.JOB:    JOB_PROMPT,
    SectionType.REPORT: REPORT_PROMPT,
}


def _section_key(section: EZTSection) -> str:
    return f"{section.type.value}:{section.name}"


def make_client(
    base_url:   str  = DEFAULT_BASE_URL,
    api_key:    str  = DEFAULT_API_KEY,
    verify_ssl: bool = True,
) -> dict:
    """Return a connection-config dict for the LLM REST endpoint.

    The returned dict is passed to convert_section / convert_all.
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


def convert_section(
    client:  dict,
    section: EZTSection,
    context: str,
    model:   str  = DEFAULT_MODEL,
    verbose: bool = False,
) -> str:
    """POST one EZT section to the LLM and return the raw COBOL response."""
    template = _PROMPT_FOR_TYPE[section.type]

    class _SafeDict(dict):
        def __missing__(self, key: str) -> str:
            return f"{{{key}}}"

    user_message = template.format_map(_SafeDict(
        context=context or "(none yet — this is the first section)",
        content=section.content,
    ))

    if verbose:
        print(f"  → [{section.type.value}] {section.name}", flush=True)

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
    JOB and REPORT sections are sent to the LLM with accumulated context.
    """
    results: Dict[str, str] = {}
    context_chunks: List[str] = []

    for section in sections:
        if section.type in _RULE_BASED:
            if section.type == SectionType.FILE_DEF:
                cobol = convert_file_def(source)
            else:
                cobol = convert_field_def(section.content)
            if verbose:
                print(f"  → [{section.type.value}] {section.name} (rule-based)", flush=True)
        else:
            context = "\n\n".join(context_chunks)
            cobol = convert_section(client, section, context, model=model, verbose=verbose)

        key = _section_key(section)
        results[key] = cobol
        if section.type in _RULE_BASED:
            context_chunks.append(
                f"=== {section.type.value.upper()} ({section.name})"
                f" — ALREADY IN DATA DIVISION, DO NOT REDECLARE ===\n{cobol}"
            )
        else:
            # Keep only the WS additions from JOB/REPORT in cross-section context.
            # Leaking the procedure code forward causes the next section's LLM
            # call to re-emit or extend those paragraphs, duplicating the logic.
            llm_ws, _ = split_ws_proc(cobol)
            if llm_ws.strip():
                context_chunks.append(
                    f"=== {section.type.value.upper()} ({section.name})"
                    f" — ALREADY DECLARED IN WORKING-STORAGE, DO NOT REDECLARE ==="
                    f"\n{llm_ws}"
                )

    return results
