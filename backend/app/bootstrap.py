"""Startup tasks and demo accounts.

    python -m app.bootstrap --seed        # create the demo user (from backend/)
"""
import argparse
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from .core.config import BACKEND_DIR, DEMO_ADMIN_PASSWORD, get_settings
from .core.security import hash_password, verify_password
from .db.models import User
from .db.session import Base, SessionLocal, engine

log = logging.getLogger(__name__)
DEMO_EMAIL, DEMO_PASSWORD = "demo@briefdesk.dev", "Research2026"


def init_db() -> None:
    """test: create_all on a throwaway DB. development: alembic upgrade head. production: refuse to start
    if the schema is behind (migrations are a deliberate release step)."""
    s = get_settings()
    if s.environment == "test":
        Base.metadata.create_all(engine)
        return
    from alembic import command
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.attributes["app_logging"] = True
    if s.environment == "development":
        command.upgrade(cfg, "head")
        return
    head = ScriptDirectory.from_config(cfg).get_current_head()
    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
    if current != head:
        raise RuntimeError(f"Database schema is at {current}, code expects {head}: run `alembic upgrade head`")


def ensure_admin(db: Session) -> None:
    s = get_settings()
    if not db.scalar(select(User.id).where(User.role == "admin")):
        db.add(User(email=s.bootstrap_admin_email.lower(), name="Admin", role="admin",
                    password_hash=hash_password(s.bootstrap_admin_password)))
        db.commit()
        log.warning("Created bootstrap admin %s - change the password!", s.bootstrap_admin_email)


def warn_insecure_defaults(db: Session) -> None:
    for admin in db.scalars(select(User).where(User.role == "admin", User.is_active.is_(True))):
        if verify_password(DEMO_ADMIN_PASSWORD, admin.password_hash):
            log.warning("SECURITY: admin %s still uses the public demo password - change it in Settings",
                        admin.email)


def seed_demo(db: Session) -> None:
    if not db.scalar(select(User.id).where(User.email == DEMO_EMAIL)):
        db.add(User(email=DEMO_EMAIL, name="Demo Analyst", role="user", password_hash=hash_password(DEMO_PASSWORD)))
        db.commit()
    print("Demo logins:")
    print(f"  user   {DEMO_EMAIL} / {DEMO_PASSWORD}")
    print(f"  admin  {get_settings().bootstrap_admin_email} / (BOOTSTRAP_ADMIN_PASSWORD)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true", help="create the demo user")
    args = ap.parse_args()
    init_db()
    with SessionLocal() as session:
        ensure_admin(session)
        if args.seed:
            seed_demo(session)
