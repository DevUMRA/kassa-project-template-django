"""Production сервері (Windows және Linux): python serve.py

Waitress HTTP-ны тек ішкі адресте тыңдайды; сыртқы HTTPS-ті reverse proxy (nginx, IIS, Caddy …) береді.
Баптаулар .env файлынан (DJANGO_* және WAITRESS_*), үлгісі — .env.example.
"""
import os

from waitress import serve

from core.wsgi import application  # .env-ті core.settings жүктейді


if __name__ == '__main__':
    serve(
        application,
        host=os.environ.get('WAITRESS_HOST', '127.0.0.1'),
        port=int(os.environ.get('WAITRESS_PORT', '8000')),
        threads=int(os.environ.get('WAITRESS_THREADS', '8')),
        # Proxy-дің X-Forwarded-Proto/For/Host тақырыптарына тек осы адрестен сенеміз;
        # басқа адрестен келген мұндай тақырыптар өшіріледі (HTTPS-ті жалған көрсетуге болмайды)
        trusted_proxy=os.environ.get('WAITRESS_TRUSTED_PROXY', '127.0.0.1'),
        trusted_proxy_headers='x-forwarded-proto x-forwarded-for x-forwarded-host',
        clear_untrusted_proxy_headers=True,
        ident='store',
    )
