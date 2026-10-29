"""Local scope, export and offline retention control. Never runs models or loads .env."""
import argparse
import sqlite3
from contextlib import closing
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.observability import Runtime
from core.observability.maintenance import export, prune


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', type=Path, default=ROOT / '.local-data/observability')
    sub = parser.add_subparsers(dest='action', required=True)
    for name in ('scope', 'export'):
        command = sub.add_parser(name)
        command.add_argument('--owner', required=True)
        command.add_argument('--session', required=True)
        if name == 'scope':
            command.add_argument('--seconds', type=int, default=300)
        else:
            command.add_argument('--content', action='store_true')
            command.add_argument('--request-id')
            command.add_argument('--output', type=Path, required=True)
    sub.add_parser('prune')
    args = parser.parse_args()
    runtime = Runtime(args.path)
    # Do not start a second processor or run crash recovery against the live writer.
    with runtime.connect() as db:
        if db.execute('pragma user_version').fetchone()[0] != 1:
            parser.error('unsupported trace schema; start the application first')
    if args.action == 'scope':
        with runtime.connect() as db:
            if db.execute('select 1 from tombstones where owner=? and session=?', (args.owner,args.session)).fetchone():
                parser.error('session was deleted')
        runtime.diagnostic(args.owner, args.session, args.seconds)
        print('Scoped diagnostics enabled; expires automatically.')
    elif args.action == 'export':
        value = export(runtime, args.owner, args.session, content=args.content, request=args.request_id)
        with open(args.output, 'x', encoding='utf-8', opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        print('Trace exported; frozen exports are outside session deletion.')
    else:
        os.close(os.open(args.path / '.writer.lock', os.O_CREAT | os.O_RDWR, 0o600))
        with closing(sqlite3.connect(args.path / '.writer.lock', timeout=0)) as lock:
            try:
                lock.execute('BEGIN EXCLUSIVE')
            except sqlite3.OperationalError:
                parser.error('stop the application before manual pruning')
            prune(runtime)
        print('Observation retention applied.')
    runtime.close()


if __name__ == '__main__':
    main()
