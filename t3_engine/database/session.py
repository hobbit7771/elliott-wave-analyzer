"""Engine/session factory + schema bootstrap."""

from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from t3_engine.database.models import Base


def normalize_database_url(database_url: str) -> str:
    """Accept the URL shapes hosting providers actually hand out.

    Render, Heroku and others print `postgres://...`, which SQLAlchemy 2
    refuses outright ("Can\'t load plugin: sqlalchemy.dialects:postgres").
    Pasting the provider\'s own string into T3_DATABASE_URL should work, so
    it is rewritten here rather than left as a trap. The driver is pinned
    to psycopg (v3) because that is what requirements.txt installs."""
    if database_url.startswith("postgres://"):
        database_url = "postgresql+psycopg://" + database_url[len("postgres://"):]
    elif database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url[len("postgresql://"):]
    return database_url


def is_durable(database_url: str) -> bool:
    """Whether data written here survives a redeploy.

    A SQLite file on a container filesystem does not: the container is
    replaced on every deploy and the file goes with it. That is not a
    detail to leave implicit - it is the difference between "saved
    analyses" and "saved analyses until the next push"."""
    return not normalize_database_url(database_url).startswith("sqlite")


def make_engine(database_url: str):
    url = normalize_database_url(database_url)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    kwargs = {"connect_args": connect_args}
    if not url.startswith("sqlite"):
        # A managed database closes idle connections; without this the
        # first query after a quiet spell fails instead of reconnecting.
        kwargs["pool_pre_ping"] = True
        kwargs["pool_recycle"] = 300
    return create_engine(url, **kwargs)


def init_db(database_url: str):
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    return engine


def make_session_factory(engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(session_factory: sessionmaker) -> Session:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
