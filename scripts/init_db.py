"""One-shot CLI: aplica migrations/001_initial.sql em data/polytrader.db."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import click

from src.core.database import DEFAULT_DB_PATH, init_database


@click.command()
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=DEFAULT_DB_PATH,
    show_default=True,
    help="Caminho do arquivo SQLite.",
)
def main(db_path: Path) -> None:
    click.echo(f"Inicializando banco em: {db_path}")
    asyncio.run(init_database(db_path))
    click.echo("OK — schema aplicado com WAL ativo.")


if __name__ == "__main__":
    main()
