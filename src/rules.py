"""Load EZT->COBOL mapping rules from YAML and format them for prompt injection."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

_RULES_DIR = Path(__file__).parent.parent / "rules"


def _load(filename: str) -> dict:
    with (_RULES_DIR / filename).open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@dataclass(frozen=True)
class CopybookHook:
    copy: str
    perform: str
    sections: tuple[str, ...]
    when: str | None


_ALLOWED_SECTIONS = ("working-storage", "procedure")
_DEFAULT_SECTIONS: tuple[str, ...] = ("procedure",)
_DEFAULT_WHEN: dict[str, str] = {
    "file_open_failure":  "WS-{file}-STATUS NOT = '00'",
    "file_close_failure": "WS-{file}-STATUS NOT = '00'",
}


def load_copybooks() -> dict[str, CopybookHook]:
    """Return {event_name: CopybookHook}.  Empty dict if copybooks.yaml is absent."""
    path = _RULES_DIR / "copybooks.yaml"
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    hooks: dict[str, CopybookHook] = {}
    for event, cfg in raw.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"copybooks.yaml: {event!r} must be a mapping")
        for required in ("copy", "perform"):
            if required not in cfg:
                raise ValueError(f"copybooks.yaml: {event!r} missing {required!r}")
        sections = tuple(cfg.get("sections") or _DEFAULT_SECTIONS)
        bad = [s for s in sections if s not in _ALLOWED_SECTIONS]
        if bad:
            raise ValueError(
                f"copybooks.yaml: {event!r} has unknown sections {bad}; "
                f"allowed: {list(_ALLOWED_SECTIONS)}"
            )
        hooks[event] = CopybookHook(
            copy=str(cfg["copy"]).strip(),
            perform=str(cfg["perform"]).strip(),
            sections=sections,
            when=cfg.get("when", _DEFAULT_WHEN.get(event)),
        )
    return hooks


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
