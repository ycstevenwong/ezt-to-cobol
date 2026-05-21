"""Load EZT->COBOL mapping rules from YAML and format them for prompt injection."""
from pathlib import Path

import yaml

_RULES_DIR = Path(__file__).parent.parent / "rules"


def _load(filename: str) -> dict:
    with (_RULES_DIR / filename).open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def general_rules_text() -> str:
    """Return LLM-facing rules for JOB/REPORT conversion only.

    FILE/FIELD/WS sections are handled by Python — their rules are not
    injected into the prompt.
    """
    rules = _load("ezt_to_cobol.yaml")
    parts = ["## EZT to COBOL Mapping Rules"]

    for key, header in [
        ("conditions",  "### Condition Operators"),
        ("statements",  "### Statement Mapping"),
        ("control_flow", "### Control Flow"),
    ]:
        if key not in rules:
            continue
        parts.append(header)
        for ezt, cobol in rules[key].items():
            parts.append(f"  {ezt} -> {cobol}")
        parts.append("")

    return "\n".join(parts)


def report_scaffolding_text() -> str:
    rules = _load("report_scaffolding.yaml")
    parts = ["## REPORT Section Conversion Rules"]

    if "overview" in rules:
        parts.append(rules["overview"].strip())
        parts.append("")

    if "required_ws" in rules:
        parts.append("### Always-Required WORKING-STORAGE")
        parts.append(rules["required_ws"].strip())
        parts.append("")

    if "required_paragraphs" in rules:
        parts.append("### Always-Required Paragraphs")
        parts.append(rules["required_paragraphs"].strip())
        parts.append("")

    if "directives" in rules:
        parts.append("### Directive Mappings")
        for directive, info in rules["directives"].items():
            parts.append(f"\n#### {directive}")
            if "description" in info:
                for line in str(info["description"]).strip().splitlines():
                    parts.append(f"  {line}")
            if "working_storage" in info:
                parts.append("  WORKING-STORAGE:")
                for line in str(info["working_storage"]).strip().splitlines():
                    parts.append(f"    {line}")
            if "procedure" in info:
                parts.append("  PROCEDURE:")
                for line in str(info["procedure"]).strip().splitlines():
                    parts.append(f"    {line}")
            if "cobol_action" in info:
                parts.append(f"  COBOL: {info['cobol_action']}")

    return "\n".join(parts)
