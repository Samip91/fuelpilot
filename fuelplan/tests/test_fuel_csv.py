"""Spec 01 FR-13, FR-14: reading the provided CSV and rejecting invalid rows."""

import csv
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path

from fuelplan.fuel_csv import EXPECTED_HEADER, CsvHeaderError, parse_row, read_records

from .base import OfflineTestCase

VALID = {
    "OPIS Truckstop ID": "7",
    "Truckstop Name": "PILOT  #7",
    "Address": "I-80,  EXIT 46",
    "City": "Walnut",
    "State": "ia",
    "Rack ID": "1",
    "Retail Price": "3.199",
}


class ParseRowTests(OfflineTestCase):
    def test_a_valid_row_is_parsed_with_whitespace_collapsed(self) -> None:
        row = parse_row(VALID)

        self.assertIsNotNone(row)
        self.assertEqual(
            (row.opis_id, row.name, row.address, row.state, row.price),
            (7, "PILOT #7", "I-80, EXIT 46", "IA", Decimal("3.199")),
        )

    def test_rows_with_an_invalid_id_are_rejected(self) -> None:
        for opis_id in ("", "abc", "0", "²", "1.5", "-4"):
            with self.subTest(opis_id=opis_id):
                self.assertIsNone(parse_row({**VALID, "OPIS Truckstop ID": opis_id}))

    def test_rows_with_an_invalid_price_are_rejected(self) -> None:
        for price in ("", "abc", "0", "-1", "100", "NaN", "Infinity", "1e1", "٣.١"):
            with self.subTest(price=price):
                self.assertIsNone(parse_row({**VALID, "Retail Price": price}))

    def test_a_price_just_below_one_hundred_is_accepted(self) -> None:
        row = parse_row({**VALID, "Retail Price": "99.999"})

        self.assertEqual(row.price, Decimal("99.999"))


class ReadRecordsTests(OfflineTestCase):
    def setUp(self) -> None:
        super().setUp()
        workdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, workdir)
        self.path = workdir / "fuel.csv"

    def test_a_utf8_byte_order_mark_is_accepted(self) -> None:
        with self.path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(EXPECTED_HEADER)
            writer.writerow(VALID.values())

        records = list(read_records(self.path))

        self.assertEqual(records[0]["OPIS Truckstop ID"], "7")

    def test_a_wrong_header_is_refused(self) -> None:
        self.path.write_text("id,name,price\n1,A,3.1\n", encoding="utf-8")

        with self.assertRaises(CsvHeaderError):
            list(read_records(self.path))
