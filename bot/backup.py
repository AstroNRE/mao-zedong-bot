"""WAL-safe SQLite online backups, run outside the event loop."""
import argparse
import asyncio
import sqlite3
from pathlib import Path


def _backup(source: Path, destination: Path):
    source, destination = source.resolve(), destination.resolve()
    if source == destination or not source.is_file():
        raise ValueError('Source must exist and differ from destination')
    if destination.exists():
        raise FileExistsError('Refusing to overwrite an existing backup')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as incoming:
        with sqlite3.connect(destination) as outgoing:
            incoming.backup(outgoing, pages=256, sleep=0.05)
            if outgoing.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Backup integrity check failed')
            if outgoing.execute('PRAGMA foreign_key_check').fetchall():
                raise RuntimeError('Backup foreign key check failed')
    destination.chmod(0o600)


async def backup_database(source: Path, destination: Path):
    await asyncio.to_thread(_backup, Path(source), Path(destination))


def main():
    parser = argparse.ArgumentParser(description='Online SQLite backup (safe with WAL)')
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    asyncio.run(backup_database(args.source, args.destination))


if __name__ == '__main__':
    main()
