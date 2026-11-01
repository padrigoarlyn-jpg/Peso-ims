"""Create a timestamped MySQL backup using mysqldump; never logs credentials.

Requires DATABASE_URL, mysqldump in PATH, and a private BACKUP_DIR.
Use an encrypted volume/object store for off-host retention.
"""
import datetime as dt
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit, unquote


def main():
    url = os.environ.get('DATABASE_URL')
    if not url:
        raise SystemExit('DATABASE_URL is required; refusing to guess database credentials.')
    parsed = urlsplit(url)
    if not parsed.scheme.startswith('mysql') or not parsed.hostname or not parsed.path.strip('/'):
        raise SystemExit('DATABASE_URL must be a MySQL connection URL.')
    if not shutil.which('mysqldump'):
        raise SystemExit('mysqldump is not installed or not in PATH.')
    backup_dir = Path(os.environ.get('BACKUP_DIR', './private_backups')).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        backup_dir.chmod(0o700)
    timestamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    dest = backup_dir / f'peso_ims_{timestamp}.sql'
    # Keep database password out of command-line arguments and generated logs.
    cfg = tempfile.NamedTemporaryFile(mode='w', suffix='.cnf', delete=False, encoding='utf-8')
    try:
        if os.name != 'nt':
            os.chmod(cfg.name, 0o600)
        def esc(value):
            return str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '')
        cfg.write('[client]\n')
        cfg.write(f'host="{esc(parsed.hostname)}"\n')
        cfg.write(f'port={parsed.port or 3306}\n')
        cfg.write(f'user="{esc(unquote(parsed.username or ""))}"\n')
        cfg.write(f'password="{esc(unquote(parsed.password or ""))}"\n')
        cfg.flush(); cfg.close()
        args = ['mysqldump', f'--defaults-extra-file={cfg.name}', '--single-transaction', '--quick', '--routines', '--triggers', '--events', '--set-charset', '--result-file='+str(dest), unquote(parsed.path.lstrip('/'))]
        subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if dest.stat().st_size == 0:
            raise RuntimeError('Empty backup created')
        if os.name != 'nt':
            dest.chmod(0o600)
        print(f'Backup created: {dest} ({dest.stat().st_size} bytes)')
    except Exception:
        dest.unlink(missing_ok=True)
        raise RuntimeError('Database backup failed; inspect mysqldump configuration and permissions.') from None
    finally:
        try: os.unlink(cfg.name)
        except OSError: pass


if __name__ == '__main__':
    main()
