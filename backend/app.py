import sys
from pathlib import Path

import click
from flask import Flask
from werkzeug.security import generate_password_hash


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(1, str(REPOSITORY_ROOT))

from config import Config
from db_repository import db_manager
from extensions import cors, db
from models import User
from runtime_core.secrets import validate_control_secrets
from routes.admin import admin_bp
from routes.auth import auth_bp
from routes.taverns import taverns_bp


def create_app(config_object=Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_object)
    validate_control_secrets(app.config)

    database_uri = app.config["SQLALCHEMY_DATABASE_URI"]
    if database_uri.startswith("sqlite:///") and ":memory:" not in database_uri:
        Path(database_uri.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    cors.init_app(app, resources={r"/api/*": {"origins": "*"}})
    app.register_blueprint(auth_bp)
    app.register_blueprint(taverns_bp)
    app.register_blueprint(admin_bp)

    @app.get("/api/health")
    def health():
        return {"code": 200, "message": "ok", "data": {"service": "ai-rpg-platform"}}

    @app.cli.command("init-db")
    def init_db_command():
        db.create_all()
        click.echo("Database initialized.")

    @app.cli.command("bootstrap-admin")
    @click.option("--username", prompt=True)
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    @click.option("--display-name", default="Platform Admin")
    def bootstrap_admin(username: str, password: str, display_name: str):
        existing = db_manager.query(User).filter_by(username=username).first()
        if existing:
            existing.is_admin = True
            existing.is_active = True
            existing.password_hash = generate_password_hash(password)
            existing.display_name = display_name
        else:
            user = User(
                username=username,
                display_name=display_name,
                password_hash=generate_password_hash(password),
                is_admin=True,
            )
            ok, error = db_manager.add(user)
            if not ok:
                raise click.ClickException(error)
        ok, error = db_manager.commit()
        if not ok:
            raise click.ClickException(error)
        click.echo(f"Admin '{username}' is ready.")

    with app.app_context():
        db.create_all()

    return app
