"""Create a private, consistent backup with the Python SQLite backup API.

Usage: python backup.py /private/path/desk-backup.zip
       python backup.py --verify /private/path/desk-backup.zip
No sqlite3 shell executable is needed. Existing files are never overwritten.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import zipfile

from app import data_directory, stamp


def verify_backup(path):
    with zipfile.ZipFile(path) as archive:
        if sorted(archive.namelist()) != ['depositdesk.sqlite3', 'manifest.json', 'owner.json']:
            raise ValueError('Unexpected backup contents.')
        manifest = json.loads(archive.read('manifest.json'))
        for name in ('depositdesk.sqlite3', 'owner.json'):
            if hashlib.sha256(archive.read(name)).hexdigest() != manifest['sha256'][name]:
                raise ValueError('Backup checksum mismatch: ' + name)
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / 'verify.sqlite3'
            database.write_bytes(archive.read('depositdesk.sqlite3'))
            with sqlite3.connect(database) as connection:
                if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Backup database integrity check failed.')
                if connection.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('Backup database has a broken relationship.')
    return manifest


def backup(directory, target):
    directory, target = Path(directory), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory() as temporary:
        snapshot = Path(temporary) / 'depositdesk.sqlite3'
        source = sqlite3.connect((directory / 'depositdesk.sqlite3').resolve().as_uri() + '?mode=ro', uri=True)
        try:
            with sqlite3.connect(snapshot) as destination:
                source.backup(destination)
                has_owner_auth = bool(destination.execute("SELECT 1 FROM metadata WHERE key='owner_auth'").fetchone())
                destination.execute('DELETE FROM sessions')
                destination.execute('DELETE FROM login_attempts')
                destination.commit()
                destination.execute('PRAGMA journal_mode=DELETE')
                if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Snapshot integrity check failed.')
        finally:
            source.close()
        original_config = json.loads((directory / 'owner.json').read_text())
        config = {'business_name': original_config.get('business_name', 'My business')}
        if not has_owner_auth:
            # Backing up v1 *before* its first v2 migration must retain the
            # bootstrap credential needed to open that older database.
            config['password'] = original_config['password']
        files = {'depositdesk.sqlite3': snapshot.read_bytes(), 'owner.json': json.dumps(config).encode()}
        manifest = {'format': 1, 'created_utc': stamp(), 'sessions_removed': True,
            'sha256': {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream:
                with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
                    for name, content in files.items():
                        archive.writestr(name, content)
                    archive.writestr('manifest.json', json.dumps(manifest, indent=2))
                stream.flush()
                os.fsync(stream.fileno())
            return verify_backup(target)
        except BaseException:
            target.unlink(missing_ok=True)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    result = verify_backup(args.path) if args.verify else backup(data_directory(), args.path)
    print(json.dumps({'verified': True, 'created_utc': result['created_utc'], 'file': str(args.path)}))


if __name__ == '__main__':
    main()
