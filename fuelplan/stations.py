"""The in-memory station index: every imported station, loaded once per process (spec 01 §6).

The request path never queries the Station table: the planner asks this index which
stations lie on a route corridor. After a re-import the server is restarted, so the
index and its `dataset_version` (part of the plan cache key, FR-40) never go stale.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction

from .errors import StationDataMissing
from .geo import RouteCorridor
from .locations import LazyValue
from .models import DatasetImport, Station
from .optimizer import Candidate

STATION_FIELDS = (
    "id",
    "name",
    "address",
    "city",
    "state",
    "price_text",
    "latitude",
    "longitude",
    "location_quality",
)


@dataclass(frozen=True, slots=True)
class StationRecord:
    """One imported station; `price_text` is the CSV text, `price_per_gallon` its exact value."""

    station_id: int
    name: str
    address: str
    city: str
    state: str
    price_text: str
    price_per_gallon: Decimal
    latitude: float
    longitude: float
    location_quality: str


class StationIndex:
    """All stations of one import, immutable, with the import's `dataset_version`."""

    def __init__(self, *, dataset_version: str, records: tuple[StationRecord, ...]) -> None:
        self._dataset_version = dataset_version
        self._records = tuple(sorted(records, key=lambda record: record.station_id))
        self._by_id: Mapping[int, StationRecord] = {
            record.station_id: record for record in self._records
        }

    @classmethod
    def from_database(cls) -> "StationIndex":
        """Read the newest import and all stations: 2 queries, one consistent snapshot.

        Raises StationDataMissing when either is absent (FR-43), so a half-imported
        database is never used.
        """
        # With IMMEDIATE transactions (settings) this read briefly takes the write lock; it
        # runs once per process (startup warm-up, else first use), so writers barely wait.
        with transaction.atomic():
            dataset_version = (
                DatasetImport.objects.order_by("-imported_at", "-id")
                .values_list("dataset_version", flat=True)
                .first()
            )
            rows = list(Station.objects.order_by("id").values_list(*STATION_FIELDS))
        if dataset_version is None or not rows:
            raise StationDataMissing()
        return cls(dataset_version=dataset_version, records=tuple(_record(row) for row in rows))

    @property
    def dataset_version(self) -> str:
        """The import's version; plans made from another import never match (FR-40)."""
        return self._dataset_version

    def __len__(self) -> int:
        return len(self._records)

    def record(self, station_id: int) -> StationRecord:
        """The station with this OPIS ID; a missing ID is a bug, so it raises KeyError."""
        return self._by_id[station_id]

    def candidates_on(self, corridor: RouteCorridor) -> tuple[Candidate, ...]:
        """Every station whose estimated detour from this route is at most 5 mi (FR-18)."""
        candidates = []
        for record in self._records:
            position = corridor.locate(latitude=record.latitude, longitude=record.longitude)
            if position is None:
                continue
            candidates.append(
                Candidate(
                    station_id=record.station_id,
                    route_mile=position.route_mile,
                    detour_miles=position.estimated_detour_miles,
                    price_per_gallon=record.price_per_gallon,
                    is_detour_verified=False,
                )
            )
        return tuple(candidates)


def _record(row: tuple[object, ...]) -> StationRecord:
    station_id, name, address, city, state, price_text, latitude, longitude, quality = row
    return StationRecord(
        station_id=int(station_id),
        name=str(name),
        address=str(address),
        city=str(city),
        state=str(state),
        price_text=str(price_text),
        price_per_gallon=Decimal(str(price_text)),
        latitude=float(latitude),
        longitude=float(longitude),
        location_quality=str(quality),
    )


# A failed load raises and is not kept: FR-43 answers until the import runs, then the next
# request loads the index. A loaded index is kept, so a re-import needs a server restart.
_station_index = LazyValue(StationIndex.from_database)


def get_station_index() -> StationIndex:
    """The process-wide index; once loaded, restart the server after a re-import.

    Loaded at server start by `planner.warm_up`, else on first use.
    """
    return _station_index.get()
