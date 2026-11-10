#!/bin/sh
# Контейнер іске қосылғанда: дерекқорды күту → миграциялар → (қаласаңыз) әкімші → негізгі процесс (CMD).
set -e

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
    # Дерекқор контейнерден кейін іске қосылуы мүмкін — 30 секундқа дейін қайталаймыз
    attempt=1
    until python manage.py migrate --noinput; do
        if [ "$attempt" -ge 10 ]; then
            echo "Миграциялар орындалмады: дерекқор қолжетімсіз" >&2
            exit 1
        fi
        echo "Дерекқор әлі дайын емес, қайталау ($attempt/10)..." >&2
        attempt=$((attempt + 1))
        sleep 3
    done
fi

# DJANGO_SUPERUSER_USERNAME және DJANGO_SUPERUSER_PASSWORD берілсе — миграциялардан кейін әкімшіні бір рет жасаймыз
if [ "${RUN_MIGRATIONS:-1}" = "1" ] && [ -n "$DJANGO_SUPERUSER_USERNAME" ] && [ -n "$DJANGO_SUPERUSER_PASSWORD" ]; then
    python manage.py shell -c "
from django.contrib.auth import get_user_model
import os
User = get_user_model()
name = os.environ['DJANGO_SUPERUSER_USERNAME']
if not User.objects.filter(username=name).exists():
    User.objects.create_superuser(name, os.environ.get('DJANGO_SUPERUSER_EMAIL', ''), os.environ['DJANGO_SUPERUSER_PASSWORD'], role='MANAGER')
    print('Әкімші жасалды:', name)
"
fi

exec "$@"
