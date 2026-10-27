"""Reading the provided fuel-price CSV into typed rows (spec 01 FR-13, FR-14)."""

import csv
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

EXPECTED_HEADER = [
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
]
MAX_PRICE = Decimal("100")
# Plain decimal only: rejects "1e1", "NaN", "Infinity", signs and non-ASCII digits.
PRICE_PATTERN = re.compile(r"\d+(\.\d+)?", re.ASCII)


class CsvHeaderError(Exception):
    """The file is not the provided fuel-price CSV."""


@dataclass(frozen=True, slots=True)
class FuelRow:
    """One valid CSV record, with whitespace collapsed and the price as a Decimal."""

    opis_id: int
    name: str
    address: str
    city: str
    state: str
    price: Decimal
    price_text: str

    @property
    def address_key(self) -> str:
        return make_address_key(address=self.address, city=self.city, state=self.state)

    @property
    def geocoding_text(self) -> str:
        return f"{self.address}, {self.city}, {self.state}, United States"


def make_address_key(*, address: str, city: str, state: str) -> str:
    """Join address parts uppercased with single spaces, so spelling noise maps to one key."""
    parts = (" ".join(part.split()).upper() for part in (address, city, state))
    return "|".join(parts)


def parse_price(text: str) -> Decimal | None:
    """Return the price if it is a plain decimal with 0 < price < 100, else None."""
    text = text.strip()
    if not PRICE_PATTERN.fullmatch(text):
        return None
    price = Decimal(text)
    if price <= 0 or price >= MAX_PRICE:
        return None
    return price


def parse_row(raw: dict[str, str]) -> FuelRow | None:
    """Turn one CSV record into a FuelRow, or None if its ID or price is invalid."""
    opis_text = (raw.get("OPIS Truckstop ID") or "").strip()
    # isascii + isdecimal: "²".isdigit() is True but int("²") raises.
    if not (opis_text.isascii() and opis_text.isdecimal()) or int(opis_text) <= 0:
        return None
    price_text = (raw.get("Retail Price") or "").strip()
    price = parse_price(price_text)
    if price is None:
        return None
    return FuelRow(
        opis_id=int(opis_text),
        name=" ".join((raw.get("Truckstop Name") or "").split()),
        address=" ".join((raw.get("Address") or "").split()),
        city=" ".join((raw.get("City") or "").split()),
        state=(raw.get("State") or "").strip().upper(),
        price=price,
        price_text=price_text,
    )


def read_records(path: Path) -> Iterator[dict[str, str]]:
    """Yield raw records after checking the header; a BOM is tolerated."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        yield from parse_records(handle, path=path)


def parse_records(lines: Iterable[str], *, path: Path) -> Iterator[dict[str, str]]:
    """Yield raw records from CSV text already read from `path` (named in errors only)."""
    reader = csv.DictReader(lines)
    if reader.fieldnames is None:
        raise CsvHeaderError(f"{path} is empty")
    if reader.fieldnames != EXPECTED_HEADER:
        raise CsvHeaderError(f"unexpected header {reader.fieldnames!r} in {path}")
    yield from reader
