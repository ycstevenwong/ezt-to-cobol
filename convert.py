#!/usr/bin/env python3
"""CLI entry point: convert Easytrieve program(s) to COBOL."""
import sys
from pathlib import Path

import click
import requests

from src.parser import parse_ezt
from src.converter import (
    convert_all, make_client,
    DEFAULT_MODEL, DEFAULT_BASE_URL, DEFAULT_API_KEY,
)
from src.assembler import assemble


def _convert_one(
    input_file: Path,
    output: Path,
    client: dict,
    model: str,
    program_name: str | None,
    verbose: bool,
    dry_run: bool,
    dump_prompt: Path | None = None,
) -> bool:
    """Convert a single EZT file. Returns True on success."""
    try:
        source = input_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        click.echo(f"Error reading {input_file}: {exc}", err=True)
        return False

    sections = parse_ezt(source)
    if not sections:
        click.echo(f"No recognisable EZT sections in {input_file.name} — skipped.", err=True)
        return False

    click.echo(f"\n{input_file.name}: {len(sections)} section(s)", err=True)
    for s in sections:
        click.echo(
            f"  {s.type.value:<20} name={s.name!r:<25} "
            f"({len(s.content.splitlines())} lines)",
            err=True,
        )

    if dry_run:
        return True

    # Use only the base name (before any extra dots) to avoid periods in PROGRAM-ID
    prog_name = program_name or input_file.stem.split(".")[0][:8].upper()

    if dump_prompt:
        convert_all(client, sections, source, model=model, verbose=verbose,
                    dump_prompt=dump_prompt)
        click.echo(f"  → wrote LLM prompt to {dump_prompt} (LLM not called)", err=True)
        return True

    # Mandatory on every real (non-dry-run, non-dump-only) conversion: the
    # exact prompt sent to the LLM is always logged next to the output, and
    # it's written BEFORE the request goes out — so if the gateway refuses
    # or errors, the prompt that triggered it is still on disk to inspect.
    prompt_log = (output or input_file).with_suffix(".prompt.txt")

    try:
        converted = convert_all(client, sections, source, model=model, verbose=verbose,
                                 prompt_log=prompt_log)
    except requests.exceptions.ConnectionError:
        click.echo(
            f"Cannot connect to LLM at {client['url']}. "
            "Check the server is running and --base-url is correct.",
            err=True,
        )
        return False
    except requests.exceptions.HTTPError as exc:
        click.echo(f"HTTP error {exc.response.status_code}: {exc}", err=True)
        return False
    except requests.exceptions.Timeout:
        click.echo("Request timed out. Try increasing --timeout or check the server.", err=True)
        return False
    finally:
        if prompt_log.exists():
            click.echo(f"  → logged LLM prompt to {prompt_log}", err=True)

    cobol = assemble(sections, converted, program_name=prog_name, source=source)

    if output:
        try:
            output.write_text(cobol, encoding="utf-8")
            click.echo(f"  → wrote {len(cobol.splitlines())} lines to {output}", err=True)
        except OSError as exc:
            click.echo(f"Error writing {output}: {exc}", err=True)
            return False
    else:
        click.echo(cobol)

    return True


@click.command()
@click.argument("input_files", nargs=-1, required=True,
                type=click.Path(exists=True, path_type=Path))
@click.option(
    "-o", "--output-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Output directory for .cbl files (default: same folder as each input).",
)
@click.option(
    "--model",
    default=DEFAULT_MODEL,
    show_default=True,
    help="LLM model name (e.g. llama3.2, qwen2.5-coder, gpt-4o)",
)
@click.option(
    "--base-url",
    default=DEFAULT_BASE_URL,
    show_default=True,
    help="LLM server base URL (OpenAI-compatible /v1 endpoint).",
)
@click.option(
    "--api-key",
    default=DEFAULT_API_KEY,
    show_default=True,
    help="API key sent in the Authorization header.",
)
@click.option(
    "--no-verify",
    is_flag=True,
    default=False,
    help="Disable SSL certificate verification (for self-signed certs).",
)
@click.option(
    "--program-name",
    default=None,
    help="COBOL PROGRAM-ID (only used when converting a single file).",
)
@click.option("-v", "--verbose", is_flag=True,
              help="Show section-by-section progress to stderr.")
@click.option("--dry-run", is_flag=True,
              help="Parse and show detected sections; do not call the model.")
@click.option(
    "--dump-prompt",
    type=click.Path(path_type=Path),
    default=None,
    help="Write the full LLM prompt (system + user message) to this file "
         "instead of calling the model. Useful for inspecting exactly what "
         "would be sent (e.g. to check against a gateway content filter).",
)
def main(input_files, output_dir, model, base_url, api_key, no_verify,
         program_name, verbose, dry_run, dump_prompt):
    """Convert one or more Easytrieve (.ezt) programs to COBOL.

    INPUT_FILES: one or more .ezt source files.

    Each file is written to <name>.cbl in OUTPUT_DIR (or the same folder as
    the input if --output-dir is not given).  Use stdout by omitting
    --output-dir only when converting a single file without -o.
    """
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    client = make_client(base_url=base_url, api_key=api_key, verify_ssl=not no_verify)
    ok = failed = 0

    for input_file in input_files:
        if output_dir:
            out = output_dir / (input_file.stem + ".cbl")
        elif len(input_files) == 1:
            out = None          # single file → stdout
        else:
            out = input_file.with_suffix(".cbl")

        if dump_prompt and len(input_files) > 1:
            dp = dump_prompt.parent / f"{dump_prompt.stem}_{input_file.stem}{dump_prompt.suffix}"
        else:
            dp = dump_prompt

        success = _convert_one(
            input_file, out, client, model,
            program_name if len(input_files) == 1 else None,
            verbose, dry_run, dp,
        )
        if success:
            ok += 1
        else:
            failed += 1

    if len(input_files) > 1:
        click.echo(f"\nDone: {ok} succeeded, {failed} failed.", err=True)
        if failed:
            sys.exit(1)


if __name__ == "__main__":
    main()
