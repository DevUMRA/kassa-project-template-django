from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from inventory.backup import BackupError, create_backup, prune_backups


class Command(BaseCommand):
    help = 'Дерекқордың резервтік көшірмесін жасайды (pg_dump) және ескі көшірмелерді өшіреді.'

    def add_arguments(self, parser):
        parser.add_argument('--dir', help=f'Көшірмелер қалтасы (әдепкі: BACKUP_DIR = {settings.BACKUP_DIR})')
        parser.add_argument(
            '--keep-days', type=int, default=settings.BACKUP_KEEP_DAYS,
            help='Осы күннен ескі көшірмелерді өшіру; 0 — өшірмеу (әдепкі: BACKUP_KEEP_DAYS)',
        )

    def handle(self, *args, dir=None, keep_days=None, **options):
        try:
            path = create_backup(dir)
        except BackupError as exc:
            raise CommandError(str(exc))
        size_mb = path.stat().st_size / 1024 / 1024
        self.stdout.write(self.style.SUCCESS(f'Көшірме жасалды: {path} ({size_mb:.2f} МБ)'))

        for old in prune_backups(keep_days, dir):
            self.stdout.write(f'Ескі көшірме өшірілді: {old.name}')
