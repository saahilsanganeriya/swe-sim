"""`IndexStore`: the writer of the index and its SQL reader (interfaces §2).

Contract
- Built from a SQLAlchemy URL (`postgresql+psycopg://...` in production; `sqlite://` in unit tests). `pool_pre_ping`
  is on; the URL is never logged or repr'd.
- `migrate()` runs the Alembic revisions under `swesim/index/migrations` to head inside one transaction.
"""
from __future__ import annotations

import warnings
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy.pool import StaticPool

from swesim.settings import Settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config() -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


def make_engine(url: str) -> sa.Engine:
    """Engine for the URL; in-memory SQLite shares one connection so every thread sees the same database."""
    if url.startswith("sqlite"):
        # SQLite is the unit-test dialect only; it converts Decimal through float and SQLAlchemy says so once per process.
        warnings.filterwarnings("ignore", message=r".*does \*not\* support Decimal objects natively.*")
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
        return sa.create_engine(url, **kwargs)
    return sa.create_engine(url, pool_pre_ping=True)


class IndexStore:
    def __init__(self, url: str | SecretStr, *, settings: Settings | None = None):
        raw = url.get_secret_value() if isinstance(url, SecretStr) else url
        self.engine = make_engine(raw)
        self._settings = settings

    def __repr__(self) -> str:  # never the URL
        return f"IndexStore(dialect={self.engine.dialect.name})"

    @classmethod
    def from_settings(cls, s: Settings) -> IndexStore | None:
        if s.database_url is None:
            return None
        return cls(s.database_url, settings=s)

    def migrate(self) -> None:
        """`alembic upgrade head` on this store's engine, in one transaction."""
        cfg = alembic_config()
        with self.engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
