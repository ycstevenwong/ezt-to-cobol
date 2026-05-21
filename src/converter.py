"""Orchestrate EZT-to-COBOL conversion: rule-based for structure, AI for logic."""
from openai import OpenAI
from typing import Dict, List

from src.parser import EZTSection, SectionType
from src.prompts import SYSTEM_PROMPT, JOB_PROMPT, REPORT_PROMPT
from src.rule_converter import convert_file_def, convert_field_def

DEFAULT_MODEL = "llama3.2"
DEFAULT_BASE_URL = "http://localhost:11434/v1"
MAX_TOKENS = 8192

_RULE_BASED = {SectionType.FILE_DEF, SectionType.FIELD_DEF}

_PROMPT_FOR_TYPE = {
    SectionType.JOB: JOB_PROMPT,
    SectionType.REPORT: REPORT_PROMPT,
}


def _section_key(section: EZTSection) -> str:
    return f"{section.type.value}:{section.name}"


def make_client(base_url: str = DEFAULT_BASE_URL) -> OpenAI:
    """Create an OpenAI-compatible client pointed at Ollama."""
    return OpenAI(base_url=base_url, api_key="ollama")


def convert_section(
    client: OpenAI,
    section: EZTSection,
    context: str,
    model: str = DEFAULT_MODEL,
    verbose: bool = False,
) -> str:
    """Call the model to convert one EZT section, returning raw COBOL text."""
    template = _PROMPT_FOR_TYPE[section.type]
    # Use format_map with a fallback dict so that LLM-facing placeholders
    # like {RPTNAME} and {FIELD} in report_scaffolding.yaml are kept as-is
    # rather than raising KeyError when only {context} and {content} are supplied.
    class _SafeDict(dict):
        def __missing__(self, key: str) -> str:
            return f"{{{key}}}"

    user_message = template.format_map(_SafeDict(
        context=context or "(none yet — this is the first section)",
        content=section.content,
    ))

    if verbose:
        print(f"  → [{section.type.value}] {section.name}", flush=True)

    parts: List[str] = []
    stream = client.chat.completions.create(
        model=model,
        max_tokens=MAX_TOKENS,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        stream=True,
    )
    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            parts.append(delta)
            if verbose:
                print(delta, end="", flush=True)

    if verbose:
        print()

    return "".join(parts).strip()


def convert_all(
    client: OpenAI,
    sections: List[EZTSection],
    source: str,
    model: str = DEFAULT_MODEL,
    verbose: bool = False,
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
            # Prefix rule-based output so the LLM knows it is already in the
            # DATA DIVISION and must not be reproduced in procedure code.
            context_chunks.append(
                f"=== {section.type.value.upper()} ({section.name})"
                f" — ALREADY IN DATA DIVISION, DO NOT REDECLARE ===\n{cobol}"
            )
        else:
            context_chunks.append(f"=== {section.type.value.upper()} ({section.name}) ===\n{cobol}")

    return results
