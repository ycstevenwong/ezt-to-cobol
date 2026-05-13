"""Orchestrate Ollama API calls to convert EZT sections to COBOL."""
from openai import OpenAI
from typing import Dict, List

from src.parser import EZTSection, SectionType
from src.prompts import (
    SYSTEM_PROMPT,
    FILE_DEF_PROMPT,
    FIELD_DEF_PROMPT,
    JOB_PROMPT,
    REPORT_PROMPT,
    MACRO_PROMPT,
)

DEFAULT_MODEL = "llama3.2"
DEFAULT_BASE_URL = "http://localhost:11434/v1"
MAX_TOKENS = 8192

_PROMPT_FOR_TYPE = {
    SectionType.FILE_DEF: FILE_DEF_PROMPT,
    SectionType.FIELD_DEF: FIELD_DEF_PROMPT,
    SectionType.JOB: JOB_PROMPT,
    SectionType.REPORT: REPORT_PROMPT,
    SectionType.MACRO: MACRO_PROMPT,
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
    user_message = template.format(
        context=context or "(none yet — this is the first section)",
        content=section.content,
    )

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
    model: str = DEFAULT_MODEL,
    verbose: bool = False,
) -> Dict[str, str]:
    """Convert every EZT section, accumulating context for each subsequent call."""
    results: Dict[str, str] = {}
    context_chunks: List[str] = []

    for section in sections:
        context = "\n\n".join(context_chunks)
        cobol = convert_section(client, section, context, model=model, verbose=verbose)
        key = _section_key(section)
        results[key] = cobol
        context_chunks.append(f"=== {section.type.value.upper()} ({section.name}) ===\n{cobol}")

    return results
