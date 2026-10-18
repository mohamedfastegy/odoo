#!/usr/bin/env python3
"""Deploy the reviewed Drive-only patch to the named staging database."""
import datetime
import pathlib
import shutil
import subprocess
import tempfile

BASE = '0b645078370604911d7cdcf02c02a277cbf2cb33'
REV = '5841be63ca041f2bb11e341492d2fd3b04e7d263'
ADDON = 'fastegy_drive_storage'
TARGET = pathlib.Path.home() / 'src/user/private_addons/fastegy_projectx' / ADDON

def run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.PIPE).stdout

def main():
    # لا تُقبل قاعدة أخرى، حتى لو نُسخ الأمر إلى طرفية اللايف بالخطأ.
    db = run('psql', '-X', '-Atc', 'select current_database()').decode().strip()
    if db != 'fastegy-staging-39633726':
        raise SystemExit('STOP: this script is restricted to the staging database.')
    if TARGET.is_symlink() or not (TARGET / '__manifest__.py').is_file():
        raise SystemExit('STOP: expected addon directory is missing.')
    with tempfile.TemporaryDirectory(prefix='fd-readiness-') as temporary:
        source = pathlib.Path(temporary) / 'source'
        run('git', 'clone', '--no-checkout', 'https://github.com/mohamedfastegy/odoo.git', str(source))
        run('git', 'cat-file', '-e', REV + '^{commit}', cwd=source)
        paths = run('git', 'diff', '--name-only', BASE, REV, '--', ADDON, cwd=source).decode().splitlines()
        changes = []
        for name in paths:
            rel = pathlib.PurePosixPath(name)
            if rel.parts[0] != ADDON or '..' in rel.parts:
                raise SystemExit('STOP: unexpected source path.')
            destination = TARGET.joinpath(*rel.parts[1:])
            new = run('git', 'show', REV + ':' + name, cwd=source)
            old = subprocess.run(['git', 'show', BASE + ':' + name], cwd=source, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if destination.is_symlink():
                raise SystemExit('STOP: unexpected symbolic link: ' + name)
            if destination.exists():
                current = destination.read_bytes()
                if current != new and (old.returncode or current != old.stdout):
                    raise SystemExit('STOP: unreviewed staging change in ' + name)
            elif old.returncode == 0:
                raise SystemExit('STOP: expected staging file missing: ' + name)
            changes.append((destination, new))
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup = pathlib.Path.home() / 'data/fastegy-drive-backups' / ('readiness-' + stamp)
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(TARGET, backup, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        backup.chmod(0o700)
        for destination, new in changes:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(new)
        print('Source backup:', backup, flush=True)
        print('Installing reviewed commit:', REV, flush=True)
        subprocess.run(['odoo-update', ADDON], check=True)
        subprocess.run(['odoosh-restart', 'http'], check=True)
        subprocess.run(['odoosh-restart', 'cron'], check=True)
        print('DEPLOYED. Native scheduler, migration, recovery and UI acceptance tests still required.')

if __name__ == '__main__':
    main()
