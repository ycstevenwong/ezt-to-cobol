"""Orchestrate EZT-to-COBOL conversion: rule-based for structure, AI for logic."""
import requests as _requests
from typing import Dict, List

from src.assembler import split_ws_proc
from src.parser import EZTSection, SectionType
from src.prompts import SYSTEM_PROMPT, LOGIC_PROMPT
from src.rule_converter import convert_file_def, convert_field_def

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


def _section_key(section: EZTSection) -> str:
    return f"{section.type.value}:{section.name}"


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"


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

    user_message = LOGIC_PROMPT.format_map(_SafeDict(
        context=context or "(none yet — no rule-based sections)",
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

    # 2. Single combined LLM call for every JOB + REPORT section.
    logic_sections = [s for s in sections if s.type not in _RULE_BASED]
    if logic_sections:
        context = "\n\n".join(context_chunks)
        results[COMBINED_LOGIC_KEY] = convert_logic(
            client, logic_sections, context, model=model, verbose=verbose
        )

    return results
