# Деплой

## Docker (Qoldanba песочницасы және жергілікті әзірлеу)

```bash
docker compose up --build
```

→ http://localhost:8000 (әкімші: `admin` / `admin-local` — тек жергілікті). Миграциялар мен статика
контейнер іске қосылғанда өздігінен орындалады, тексеру: `curl http://localhost:8000/api/health/`.

Qoldanba песочницасында қолданба `Dockerfile` бойынша жиналады, дерекқор мен айнымалыларды платформа
береді (`qoldanba.yaml`), `main`-ге әр merge — жаңа деплой. Толығырақ: [TEMPLATE.md](TEMPLATE.md).
Дерекқордың резервтік көшірмесін песочницада платформа жасайды (`backup_db` контейнерде қолданылмайды).

Төменде — Docker-сіз, жеке серверге (Windows/Linux) орнату.

## Docker-сіз орнату

Схема: **reverse proxy (HTTPS)** → **waitress** (`serve.py`, 127.0.0.1:8000) → Django → PostgreSQL.
Статиканы (админ, Swagger) қолданбаның өзі береді (whitenoise), бөлек баптау керек емес.

## 1. Орнату

```bash
python -m venv venv
venv\Scripts\activate          # Linux: source venv/bin/activate
pip install -r requirements.txt
```

## 2. PostgreSQL

`postgres` суперпайдаланушысының орнына қолданбаға бөлек пайдаланушы ашыңыз:

```sql
CREATE USER store_app WITH PASSWORD '...';
CREATE DATABASE store_db OWNER store_app;
```

## 3. Баптаулар (`.env`)

`.env.example` файлын `.env` деп көшіріп, толтырыңыз. Ең маңыздылары:

| Айнымалы | Мәні |
|---|---|
| `DJANGO_DEBUG` | `False` |
| `DJANGO_SECRET_KEY` | Жаңа кілт: `python -c "from django.core.management.utils import get_random_secret_key as g; print(g())"` |
| `DJANGO_ALLOWED_HOSTS` | Домен немесе IP: `store.example.kz` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://store.example.kz` |
| `DB_*` | 2-қадамдағы дерекқор |

`DJANGO_SECRET_KEY`, `DB_PASSWORD` немесе (production-да) `DJANGO_ALLOWED_HOSTS` жоқ болса, қолданба іске қосылмайды.

## 4. Дерекқор, статика, тексеру

```bash
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
python manage.py check --deploy
```

`check --deploy` тек `security.W021` (HSTS preload) ескертуін көрсетуі тиіс — бұл әдейі өшірулі.

## 5. Іске қосу

```bash
python serve.py
```

Тексеру: `curl -H "X-Forwarded-Proto: https" http://127.0.0.1:8000/api/health/` → `{"status":"ok","database":"ok"}`.

Windows-та қызмет (service) ретінде іске қосу үшін, мысалы, [NSSM](https://nssm.cc/): `nssm install store <жол>\venv\Scripts\python.exe serve.py` (жұмыс қалтасы — жоба түбірі).
Linux-та — systemd unit (`ExecStart=<жол>/venv/bin/python serve.py`, `WorkingDirectory=<жоба>`).

## 6. Reverse proxy (HTTPS)

Proxy HTTPS-ті қабылдап, `127.0.0.1:8000`-ге жібереді және мына тақырыптарды қояды:
`X-Forwarded-Proto: https`, `X-Forwarded-For`, `Host`. Мысалы, nginx:

```nginx
server {
    listen 443 ssl;
    server_name store.example.kz;
    # ssl_certificate ... ; ssl_certificate_key ... ;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

Proxy бөлек машинада болса, `.env`-те `WAITRESS_HOST=0.0.0.0` және `WAITRESS_TRUSTED_PROXY=<proxy IP>` қойыңыз.
`X-Forwarded-*` тақырыптарына тек осы адрестен сенеді — басқа адрестен HTTPS-ті жалған көрсету мүмкін емес.

HTTPS тұрақты жұмыс істегеніне көз жеткізген соң HSTS-ті қосыңыз: `DJANGO_SECURE_HSTS_SECONDS=3600`, кейін `31536000`.

## 7. Production-дағы айырмашылықтар

- HTTP сұраныстары HTTPS-ке бағытталады (301); cookie-лер тек HTTPS арқылы (`Secure`).
- API тек JSON қайтарады (DRF браузерлік беті өшірулі); құжаттама — `/api/docs/` (кіргеннен кейін).
- Статика файл атына хэш қосылып, ұзақ кэштеледі.
- Хаттар SMTP арқылы (`EMAIL_*`); әзірлеуде — консольға.
- Логтар консольға (`DJANGO_LOG_LEVEL`); 500 қателері `django.request` логында.
- Мониторинг: `GET /api/health/` (аутентификациясыз; дерекқор жауап бермесе — 503).

## 8. Резервтік көшірмелер

```bash
python manage.py backup_db                 # көшірме жасау + BACKUP_KEEP_DAYS-тен ескілерін өшіру
python manage.py backup_db --keep-days 0   # ештеңе өшірмеу
```

- Файл: `BACKUP_DIR\store_YYYYMMDD_HHMMSS.dump` (`pg_dump` custom форматы, сығылған).
- Әр көшірме жасалған соң `pg_restore --list` арқылы тексеріледі; дамп сәтсіз болса, жартылай файл қалмайды.
- Ең жаңа көшірме мерзімі өтсе де өшірілмейді.
- `pg_dump` PATH-та болмаса — `.env`-те `BACKUP_PG_BIN=C:\Program Files\PostgreSQL\18\bin`.
- Құпия сөз `PGPASSWORD` орта айнымалысы арқылы беріледі (командалық жолда көрінбейді).

**Күн сайын автоматты түрде (Windows Task Scheduler)** — мысалы, түнгі 02:00-де:

```bat
schtasks /Create /TN "StoreBackup" /SC DAILY /ST 02:00 /RU SYSTEM ^
  /TR "\"X:\store_system\venv\Scripts\python.exe\" \"X:\store_system\manage.py\" backup_db"
schtasks /Run /TN "StoreBackup"            &REM бірден тексеру
```

Linux (cron): `0 2 * * * cd /srv/store && venv/bin/python manage.py backup_db >> backups/backup.log 2>&1`

**Маңызды:**
- `BACKUP_DIR` басқа дискіде/серверде болсын; жүйелі түрде сыртқы тасымалдағышқа немесе бұлтқа көшіріңіз — сервер толық істен шықса, сол сервердегі көшірмелер де жоғалады.
- Көшірмеде пайдаланушылардың құпия сөз хэштері мен API токендері бар — қалтаға қол жеткізуді шектеңіз.
- Көшірменің жарамдылығын айына бір рет қалпына келтіріп тексеріңіз (төменде).

### Қалпына келтіру

```bash
# 1. Қолданбаны тоқтатыңыз (serve.py / қызмет)
python manage.py restore_db                                       # бар көшірмелер тізімі
python manage.py restore_db backups\store_20261028_020000.dump --yes
# 2. Қолданбаны қайта іске қосыңыз
```

- `--yes` болмаса, ештеңе өзгермейді.
- Қалпына келтірмес бұрын ағымдағы дерекқордың көшірмесі автоматты жасалады (қате файл таңдалса, қайта оралуға болады); өшіру үшін — `--no-safety-backup`.
- Бір транзакцияда орындалады: қате болса, дерекқор бұрынғы күйінде қалады.

Көшірмені негізгі базаға тимей тексеру (бөлек базаға):

```bash
createdb -U store_app store_check
pg_restore -U store_app --no-owner --dbname store_check backups\store_20261028_020000.dump
dropdb -U store_app store_check
```

## Жаңарту

```bash
python manage.py backup_db                 # жаңартудан бұрын көшірме
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
# serve.py-ды қайта іске қосу
```
