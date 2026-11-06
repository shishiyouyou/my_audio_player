from __future__ import annotations

import os

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import LicenseCode, LocalTrack, MobileSearchRecord, TrendingTrack


MODELS = [TrendingTrack, LocalTrack, LicenseCode, MobileSearchRecord]


def main() -> None:
    sqlite_url = os.getenv("SQLITE_DATABASE_URL", "sqlite:///./data/music_hub.db")
    postgres_url = os.getenv("DATABASE_URL", "")
    if not postgres_url or postgres_url.startswith("sqlite"):
        raise SystemExit("Set DATABASE_URL to a PostgreSQL URL before running this script.")

    source_engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})
    target_engine = create_engine(postgres_url, pool_pre_ping=True)
    Base.metadata.create_all(bind=target_engine)

    SourceSession = sessionmaker(bind=source_engine, autoflush=False, autocommit=False)
    TargetSession = sessionmaker(bind=target_engine, autoflush=False, autocommit=False)

    source = SourceSession()
    target = TargetSession()
    try:
        for model in MODELS:
            copied = 0
            for row in source.scalars(select(model)).all():
                if target.get(model, row.id):
                    continue
                values = {column.name: getattr(row, column.name) for column in model.__table__.columns}
                target.add(model(**values))
                copied += 1
            target.commit()
            print(f"{model.__tablename__}: copied {copied}")
        _reset_postgres_sequences(target_engine)
    finally:
        source.close()
        target.close()


def _reset_postgres_sequences(engine) -> None:
    if not engine.url.get_backend_name().startswith("postgresql"):
        return
    with engine.begin() as conn:
        for model in MODELS:
            table = model.__tablename__
            conn.execute(
                text(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table}), 1), true)"
                )
            )


if __name__ == "__main__":
    main()
