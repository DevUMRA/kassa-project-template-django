"""PostgreSQL резервтік көшірмелері: pg_dump (custom формат) арқылы жасау, ескілерін өшіру, қалпына келтіру."""

import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connections

PREFIX = 'store_'
SUFFIX = '.dump'
PARTIAL = '.partial'
TIMESTAMP = '%Y%m%d_%H%M%S'


class BackupError(Exception):
    pass


def _db():
    return connections['default'].settings_dict


def _tool(name):
    """pg_dump/pg_restore жолы: BACKUP_PG_BIN қалтасынан, болмаса PATH-тан."""
    bin_dir = settings.BACKUP_PG_BIN
    path = shutil.which(name, path=bin_dir) if bin_dir else shutil.which(name)
    if not path:
        raise ImproperlyConfigured(
            f'{name} табылмады. PostgreSQL bin қалтасын PATH-қа қосыңыз немесе .env-те BACKUP_PG_BIN орнатыңыз.'
        )
    return path


def _connection_args():
    db = _db()
    return ['--host', db['HOST'] or 'localhost', '--port', str(db['PORT'] or 5432), '--username', db['USER']]


def _env():
    # Құпия сөз командалық жолда емес, ортада беріледі — процестер тізімінде көрінбейді
    env = os.environ.copy()
    env['PGPASSWORD'] = _db()['PASSWORD'] or ''
    env['PGCONNECT_TIMEOUT'] = '10'
    return env


def _run(args):
    result = subprocess.run(args, env=_env(), capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode != 0:
        raise BackupError(f'{Path(args[0]).name} қатемен аяқталды ({result.returncode}): {result.stderr.strip()}')
    return result


def backup_dir(directory=None):
    path = Path(directory or settings.BACKUP_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_backups(directory=None):
    """Дайын көшірмелер, жаңасы бірінші."""
    return sorted(backup_dir(directory).glob(f'{PREFIX}*{SUFFIX}'), reverse=True)


def create_backup(directory=None, now=None):
    """Дерекқордың толық көшірмесін жасайды және жарамдылығын тексереді. Файл жолын қайтарады.

    Алдымен .partial файлына жазылады: дамп сәтсіз болса, жартылай файл дайын көшірме болып саналмайды.
    """
    directory = backup_dir(directory)
    stamp = (now or datetime.now()).strftime(TIMESTAMP)
    target = directory / f'{PREFIX}{stamp}{SUFFIX}'
    partial = target.with_name(target.name + PARTIAL)
    try:
        _run([_tool('pg_dump'), *_connection_args(), '--format=custom', '--no-password',
              '--file', str(partial), _db()['NAME']])
        # pg_restore --list файлдың оқылатынын тексереді (бүлінген архивте қате береді)
        _run([_tool('pg_restore'), '--list', str(partial)])
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return target


def prune_backups(keep_days, directory=None, now=None):
    """keep_days күннен ескі көшірмелерді өшіреді, бірақ ең жаңасын әрқашан қалдырады. Өшірілгендер тізімін қайтарады."""
    if keep_days <= 0:
        return []
    cutoff = (now or datetime.now()) - timedelta(days=keep_days)
    removed = []
    for path in list_backups(directory)[1:]:
        stamp = path.name[len(PREFIX):-len(SUFFIX)]
        try:
            created = datetime.strptime(stamp, TIMESTAMP)
        except ValueError:
            continue  # атауы біздің үлгіге сай емес — қолмен қойылған файлды өшірмейміз
        if created < cutoff:
            path.unlink()
            removed.append(path)
    return removed


def restore_backup(path):
    """Көшірмені ағымдағы дерекқорға қалпына келтіреді (бар кестелер ауыстырылады).

    --single-transaction: қате болса, ешнәрсе өзгермейді.
    """
    path = Path(path)
    if not path.is_file():
        raise BackupError(f'Файл табылмады: {path}')
    connections.close_all()
    _run([_tool('pg_restore'), *_connection_args(), '--no-password', '--clean', '--if-exists',
          '--no-owner', '--single-transaction', '--dbname', _db()['NAME'], str(path)])
