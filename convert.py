#!/usr/bin/env python3
"""CLI entry point: convert an Easytrieve program to COBOL via Ollama."""
import sys
from pathlib import Path

import click
from openai import APIConnectionError, APIStatusError

from src.parser import parse_ezt
from src.converter import convert_all, make_client, DEFAULT_MODEL, DEFAULT_BASE_URL
from src.assembler import assemble


@click.command()
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "-o", "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="Output COBOL file path (default: stdout)",
)
@click.option(
    "--model",
    default=DEFAULT_MODEL,
    show_default=True,
    help="Ollama model name (e.g. llama3.2, qwen2.5-coder, codellama)",
)
@click.option(
    "--base-url",
    default=DEFAULT_BASE_URL,
    show_default=True,
    help="Ollama server base URL",
)
@click.option(
    "--program-name",
    default=None,
    help="COBOL PROGRAM-ID (default: derived from input filename)",
)
@click.option(
    "-v", "--verbose",
    is_flag=True,
    help="Stream section-by-section output to stderr",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Parse and show detected sections; do not call the model",
)
def main(input_file, output, model, base_url, program_name, verbose, dry_run):
    """Convert an Easytrieve (EZT) program to COBOL using a local Ollama model.

    INPUT_FILE: path to the .ezt source file.

    Requires Ollama running at BASE_URL (default: http://localhost:11434).
    """
    # Read source
    try:
        source = input_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        click.echo(f"Error reading {input_file}: {exc}", err=True)
        sys.exit(1)

    # Parse
    sections = parse_ezt(source)
    if not sections:
        click.echo("No recognisable EZT sections found in the input file.", err=True)
        sys.exit(1)

    click.echo(f"Parsed {len(sections)} section(s) from {input_file.name}:", err=True)
    for s in sections:
        click.echo(
            f"  {s.type.value:<20} name={s.name!r:<25} "
            f"({len(s.content.splitlines())} lines)",
            err=True,
        )

    if dry_run:
        return

    # Derive program name
    prog_name = program_name or input_file.stem[:8].upper()

    # Convert
    click.echo(f"\nConverting with model '{model}' at {base_url} ...", err=True)
    client = make_client(base_url)
    try:
        converted = convert_all(client, sections, model=model, verbose=verbose)
    except APIConnectionError:
        click.echo(
            f"Cannot connect to Ollama at {base_url}. "
            "Make sure Ollama is running (`ollama serve`).",
            err=True,
        )
        sys.exit(1)
    except APIStatusError as exc:
        click.echo(f"API error {exc.status_code}: {exc.message}", err=True)
        sys.exit(1)

    # Assemble
    cobol = assemble(sections, converted, program_name=prog_name)

    # Emit
    if output:
        try:
            output.write_text(cobol, encoding="utf-8")
            click.echo(f"\nWrote {len(cobol.splitlines())} lines to {output}", err=True)
        except OSError as exc:
            click.echo(f"Error writing {output}: {exc}", err=True)
            sys.exit(1)
    else:
        click.echo(cobol)


if __name__ == "__main__":
    main()
