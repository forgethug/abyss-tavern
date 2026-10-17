from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()


class RuntimeEventSink:
    """Polling runtimes accept event calls without exposing a socket server."""

    @staticmethod
    def emit(_event, _payload=None, **_kwargs):
        return None


socketio = RuntimeEventSink()
