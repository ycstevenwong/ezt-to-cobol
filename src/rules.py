"""Load EZT->COBOL mapping rules from YAML and format them for prompt injection."""
from pathlib import Path

import yaml

_RULES_DIR = Path(__file__).parent.parent / "rules"


def _load(filename: str) -> dict:
    with (_RULES_DIR / filename).open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def general_rules_text() -> str:
    rules = _load("ezt_to_cobol.yaml")
    parts = ["## EZT to COBOL Mapping Rules"]

    # Data types — nested dict with cobol/note/example keys
    if "data_types" in rules:
        parts.append("\n### Data Type Mapping")
        parts.append("Format: fieldname  start-col  length  type  [decimal-places]")
        for ezt, info in rules["data_types"].items():
            if isinstance(info, dict):
                parts.append(f"\n  {ezt} -> {info['cobol']}")
                if "note" in info:
                    for line in str(info["note"]).strip().splitlines():
                        parts.append(f"       {line}")
                if "physical_bytes" in info:
                    parts.append(f"       Physical bytes: {info['physical_bytes']}")
                if "example" in info:
                    parts.append(f"       Example: {info['example']}")
            else:
                parts.append(f"  {ezt} -> {info}")
        parts.append("")

    # Field definition rules (sequential vs REDEFINES)
    if "field_definitions" in rules:
        fd = rules["field_definitions"]
        parts.append("### Field Definition Rules")
        if "syntax" in fd:
            parts.append(f"  Syntax: {fd['syntax']}")

        if "file_fields" in fd:
            ff = fd["file_fields"]
            if "description" in ff:
                for line in str(ff["description"]).strip().splitlines():
                    parts.append(f"  {line}")
            if "sequential_rule" in ff:
                parts.append("\n  Sequential fields (no overlap):")
                for line in str(ff["sequential_rule"]).strip().splitlines():
                    parts.append(f"    {line}")
            if "redefines_rule" in ff:
                parts.append("\n  Overlapping fields -> REDEFINES:")
                for line in str(ff["redefines_rule"]).strip().splitlines():
                    parts.append(f"    {line}")

        if "ws_fields" in fd:
            ws = fd["ws_fields"]
            parts.append("\n  WORKING-STORAGE fields (DEFINE — no position, no REDEFINES):")
            if "syntax" in ws:
                parts.append(f"    Syntax: {ws['syntax']}")
            if "example" in ws:
                for line in str(ws["example"]).strip().splitlines():
                    parts.append(f"    {line}")
        parts.append("")

    # Simple key->value sections
    simple_sections = [
        ("file_organization", "### File Organization"),
        ("conditions",        "### Condition Operators"),
        ("statements",        "### Statement Mapping"),
        ("control_flow",      "### Control Flow"),
    ]
    for key, header in simple_sections:
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
