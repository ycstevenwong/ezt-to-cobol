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
    """One event -> {copybook for WS, copybook for PROCEDURE, paragraph, guard}.

    copy_ws / copy_procedure are independent: a shop that puts WS items
    (constants, error-message buffer) in one copybook and paragraphs in
    another sets two different names.  Set either to None to skip that
    division entirely; at least one must be present.
    """
    copy_ws: str | None
    copy_procedure: str | None
    perform: str
    when: str | None


_DEFAULT_WHEN: dict[str, str] = {
    "file_open_failure":  "WS-{file}-STATUS NOT = '00'",
    "file_close_failure": "WS-{file}-STATUS NOT = '00'",
}


def _opt_str(cfg: dict, key: str) -> str | None:
    v = cfg.get(key)
    return str(v).strip() if v is not None else None


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
        if "perform" not in cfg:
            raise ValueError(f"copybooks.yaml: {event!r} missing 'perform'")
        copy_ws        = _opt_str(cfg, "copy_ws")
        copy_procedure = _opt_str(cfg, "copy_procedure")
        if not copy_ws and not copy_procedure:
            raise ValueError(
                f"copybooks.yaml: {event!r} needs at least one of "
                f"'copy_ws' or 'copy_procedure'"
            )
        hooks[event] = CopybookHook(
            copy_ws=copy_ws,
            copy_procedure=copy_procedure,
            perform=str(cfg["perform"]).strip(),
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
