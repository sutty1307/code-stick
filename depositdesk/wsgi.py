"""WSGI entry point.

    gunicorn --workers 2 --threads 4 --bind 127.0.0.1:8765 wsgi:application
    waitress-serve --listen 127.0.0.1:8765 wsgi:application

The store is SQLite in WAL mode. Concurrent writers are serialised with
BEGIN IMMEDIATE and a busy timeout, so a couple of workers are fine and a couple
of dozen are not. Threads are cheaper than workers here: each request opens and
closes its own connection.

Run `python app.py init` once before starting a server, or the import fails
loudly rather than serving an app with no owner.
"""
from app import create_app

application = create_app()
