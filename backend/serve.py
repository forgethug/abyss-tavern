"""Supported single-process, threaded production entrypoint."""
import os
from pathlib import Path
import signal

from app import create_app
from config import Config
from extensions import db
from flask import abort, send_from_directory
from models import Tavern
from runtime_core.process_lock import instance_lock
from services.runtime_supervisor_service import RuntimeSupervisorService
from waitress import create_server


def register_frontend(app, directory=None):
    directory = Path(directory or Path(__file__).resolve().parents[1] / 'frontend-v2/dist').resolve()
    if not (directory / 'index.html').is_file():
        raise RuntimeError('Build frontend first: cd frontend-v2 && npm ci && npm run build')

    @app.get('/')
    @app.get('/<path:path>')
    def frontend(path=''):
        if path == 'api' or path.startswith('api/'):
            abort(404)
        candidate = (directory / path).resolve()
        if not candidate.is_relative_to(directory):
            abort(404)
        if candidate.is_file():
            response = send_from_directory(directory, path)
        elif Path(path).suffix or path.startswith('assets/'):
            abort(404)
        else:
            response = send_from_directory(directory, 'index.html')
        if candidate.name == 'index.html' or not candidate.is_file():
            response.headers['Cache-Control'] = 'no-cache'
        return response


def stop_owned_runtimes(app):
    failures = []
    with app.app_context():
        for namespace in list(RuntimeSupervisorService._processes):
            tavern = db.session.query(Tavern).filter_by(runtime_namespace=namespace).first()
            if tavern:
                ok, error = RuntimeSupervisorService.stop_runtime(tavern)
                if not ok:
                    failures.append(error)
    if failures:
        raise RuntimeError('Unable to stop all owned runtimes; maintenance must remain blocked')


def main():
    with instance_lock(Config.RUNTIME_DATA_DIR):
        app = create_app()
        register_frontend(app)
        server = create_server(app, host=os.getenv('PLATFORM_HOST', '127.0.0.1'),
                               port=int(os.getenv('PLATFORM_PORT', '5500')), threads=16)
        print(f"深渊酒馆已启动：http://{os.getenv('PLATFORM_HOST', '127.0.0.1')}:{int(os.getenv('PLATFORM_PORT', '5500'))}", flush=True)
        def stop(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        try:
            server.run()
        finally:
            server.close()
            stop_owned_runtimes(app)


if __name__ == '__main__':
    main()
