from __future__ import annotations

from app.db import engine
from app.models import LicenseCode, LocalTrack, MobileSearchRecord


INDEXED_TABLES = [
    LocalTrack.__table__,
    LicenseCode.__table__,
    MobileSearchRecord.__table__,
]


def main() -> None:
    for table in INDEXED_TABLES:
        for index in table.indexes:
            index.create(bind=engine, checkfirst=True)
            print(f"ensured {index.name}")


if __name__ == "__main__":
    main()
