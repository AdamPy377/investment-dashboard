"""Daily SQLite online backups and document copies, kept outside the Git checkout."""
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


logging.basicConfig(level=logging.INFO)
data_dir = Path(os.getenv('DATA_DIR', '/data'))
backup_dir = Path(os.getenv('BACKUP_DIR', '/backups'))
backup_dir.mkdir(parents=True, exist_ok=True)
retention = max(1, int(os.getenv('BACKUP_RETENTION_DAYS', '30')))


def backup_once():
    source = data_dir / 'portfolio.sqlite3'
    if not source.exists():
        logging.info('Database has not been created yet')
        return
    timestamp = datetime.now(ZoneInfo('Australia/Melbourne')).strftime('%Y%m%d-%H%M%S')
    temp = backup_dir / (timestamp + '.partial')
    dest = backup_dir / timestamp
    temp.mkdir()
    try:
        with sqlite3.connect(f'file:{source}?mode=ro', uri=True) as live:
            with sqlite3.connect(temp / 'portfolio.sqlite3') as copy:
                live.backup(copy)
        if (data_dir / 'documents').exists():
            shutil.copytree(data_dir / 'documents', temp / 'documents')
        temp.rename(dest)
        logging.info('Backup ready at %s', dest)
        cutoff = datetime.now(ZoneInfo('Australia/Melbourne')) - timedelta(days=retention)
        for old in backup_dir.iterdir():
            if old.is_dir() and not old.name.endswith('.partial'):
                try:
                    if datetime.strptime(old.name, '%Y%m%d-%H%M%S') < cutoff.replace(tzinfo=None):
                        shutil.rmtree(old)
                except ValueError:
                    pass
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise


if __name__ == '__main__':
    while True:
        try:
            backup_once()
        except Exception:
            logging.exception('Backup failed')
        time.sleep(24 * 60 * 60)
