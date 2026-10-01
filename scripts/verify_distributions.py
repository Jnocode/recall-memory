"""Inspect built wheel/sdist members; never ship local state with code."""
from __future__ import annotations
import argparse
from pathlib import Path, PurePosixPath
import tarfile
import zipfile

FORBIDDEN_SUFFIXES = ('.db', '.sqlite', '.sqlite3', '.db-wal', '.db-shm', '.log', '.jsonl', '.pem', '.key')
FORBIDDEN_NAMES = {'MEMORY.md', 'USER.md', 'auth.json', '.env', 'config.yaml'}


def inspect_distribution(path: Path, package: str) -> list[str]:
    if path.suffix == '.whl':
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
        prefix = package + '/'
    else:
        with tarfile.open(path, 'r:gz') as archive:
            names = archive.getnames()
        prefix = names[0].split('/')[0] + '/src/' + package + '/'
    bad = [name for name in names if (
        name.lower().endswith(FORBIDDEN_SUFFIXES)
        or PurePosixPath(name).name in FORBIDDEN_NAMES
        or '.git' in PurePosixPath(name).parts
        or any(part in {'logs', 'backups', '.venv', 'node_modules'} for part in PurePosixPath(name).parts)
    )]
    if bad:
        raise ValueError(f'Forbidden distribution members: {bad}')
    required = ['__init__.py', 'memory_policy.py'] if package == 'recall_memory_hermes' else [
        '__init__.py', 'store.py', 'retrieve.py', 'embed.py', 'config.py', 'cli.py',
        'migrations.py', 'mcp_repository.py', 'authority_lock.py',
    ]
    missing = [prefix + module for module in required if prefix + module not in names]
    if missing:
        raise ValueError(f'Missing package modules: {missing}')
    return names


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--package', default='recall', choices=['recall', 'recall_memory_hermes'])
    args = parser.parse_args()
    distributions = sorted(args.directory.glob('*.whl')) + sorted(args.directory.glob('*.tar.gz'))
    if len(distributions) != 2:
        raise ValueError('Expected exactly one wheel and one sdist in a clean output directory')
    for path in distributions:
        names = inspect_distribution(path, args.package)
        print(f'DISTRIBUTION_OK {path.name} members={len(names)}')


if __name__ == '__main__':
    main()
