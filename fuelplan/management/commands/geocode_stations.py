"""Owner-only: geocode each unique station address once with Geoapify (spec 01 FR-17).

Stores raw provider fields in `data/station_locations.csv`; labels are applied later by
`import_fuel_stations`. Never runs in the request path and the reviewer never needs it.

Credits are protected in three ways:
- a credit budget (`--max-credits`) is checked before every job is created and before
  every result request;
- every created job is saved in `<out>.jobs.json` before waiting, so `--resume` fetches
  it again instead of paying for the same addresses twice;
- results are appended (and synced to disk) after each job, so a failure later loses
  nothing already paid for;
- one run at a time per output file (`<out>.lock`), so two runs never share a state file.
"""

import argparse
import csv
import fcntl
import io
import json
import os
import re
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from fuelplan.fuel_csv import CsvHeaderError, parse_row, read_records
from fuelplan.geocoding import (
    MAX_BATCH_SIZE,
    GeoapifyBatchClient,
    GeocodeResult,
    GeocodingError,
    JobFetch,
    match_results,
)
from fuelplan.http import HttpClientTransport, Transport
from fuelplan.regions import is_in_service_area

DEFAULT_CSV = Path(settings.BASE_DIR) / "data" / "raw" / "fuel-prices.csv"
DEFAULT_OUT = Path(settings.BASE_DIR) / "data" / "station_locations.csv"
OUTPUT_COLUMNS = [
    "address_key",
    "lat",
    "lon",
    "result_type",
    "confidence",
    "state_code",
    "source",
    "attribution",
    "license",
    "source_url",
]
SOURCE = "geoapify"

# Credits (FACTS: 0.5 per address, 1 per job creation, 1 per result request). Whether a
# "still running" poll costs a credit is unknown (FACTS Q-13), so every poll is counted.
CREDITS_PER_ADDRESS = Decimal("0.5")
CREDITS_PER_REQUEST = Decimal("1")
# Room for extra "still running" polls, used only to decide whether a new job may be
# submitted; every single poll is then checked against the budget on its own.
POLL_RESERVE_CREDITS = Decimal("5")
DEFAULT_MAX_CREDITS = 2_800  # under the 3,000/day free plan, with a margin

# Addresses near highways are the hard cases for a geocoder, so a sample includes them.
# Matched on the address part of the key, which is uppercased: "I-80, EXIT 46",
# "US-30 & SR-2", "HWY 50", "I-10/I-20".
HIGHWAY_STYLE_PATTERN = re.compile(r"\bEXIT\b|\bI-\d|\bUS-\d|\bSR-|\bHWY|&|/")


class RunStopped(Exception):
    """The run stops early on purpose (budget reached, job still running); work is kept."""


class CreditBudget:
    """Estimated Geoapify credits used by this run, so it stops before passing the limit."""

    def __init__(self, *, limit: int) -> None:
        self.limit = Decimal(limit)
        self.spent = Decimal(0)

    def allows(self, estimate: Decimal) -> bool:
        return self.spent + estimate <= self.limit

    def charge(self, credits: Decimal) -> None:
        self.spent += credits

    def requests_left(self) -> int:
        """Whole result requests (1 credit each, Q-13 assumption) that still fit the limit."""
        return max(0, int((self.limit - self.spent) // CREDITS_PER_REQUEST))


def new_job_estimate(address_count: int) -> Decimal:
    """Worst expected cost of one new job: addresses + creation + result + poll reserve."""
    requests = CREDITS_PER_REQUEST * 2  # job creation and the final result request
    return CREDITS_PER_ADDRESS * address_count + requests + POLL_RESERVE_CREDITS


class PendingJobsFile:
    """Jobs created but not yet saved to the output, as `{job_id: [address_key, ...]}`.

    Written atomically (temp file + rename), so a crash leaves the old or the new
    content, never half a file. The file is removed when no job is pending.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.jobs: dict[str, list[str]] = self._load()

    def in_progress_keys(self) -> set[str]:
        return {key for keys in self.jobs.values() for key in keys}

    def add(self, *, job_id: str, keys: Sequence[str]) -> None:
        self.jobs[job_id] = list(keys)
        self._write()

    def remove(self, job_id: str) -> None:
        del self.jobs[job_id]
        self._write()

    def _load(self) -> dict[str, list[str]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise CommandError(f"cannot read the pending-jobs file {self.path}: {error}") from None
        if not _is_jobs_mapping(data):
            raise CommandError(
                f"{self.path} is not a pending-jobs file (expected {{job_id: [address_key]}})"
            )
        return data

    def _write(self) -> None:
        if not self.jobs:
            self.path.unlink(missing_ok=True)
            return
        temp_path = self.path.with_name(f"{self.path.name}.tmp")
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(self.jobs, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        except OSError as error:
            raise CommandError(f"cannot save the pending-jobs file {self.path}: {error}") from None


def _is_jobs_mapping(data: object) -> bool:
    return isinstance(data, dict) and all(
        isinstance(keys, list) and keys and all(isinstance(key, str) for key in keys)
        for keys in data.values()
    )


def pending_jobs_path(out_path: Path) -> Path:
    """The pending-jobs file sits next to the output: `<out>.jobs.json`."""
    return out_path.with_name(f"{out_path.name}.jobs.json")


@contextmanager
def exclusive_run_lock(out_path: Path) -> Iterator[None]:
    """Hold `<out>.lock` for the whole run; a second run fails at once instead of waiting.

    The lock file is left in place: the kernel drops the lock when the file is closed,
    even after a crash, and deleting the file could let two runs lock different files.
    """
    lock_path = out_path.with_name(f"{out_path.name}.lock")
    with lock_path.open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CommandError("another geocode_stations run is active") from None
        yield


def collect_addresses(csv_path: Path) -> dict[str, str]:
    """Map each unique address key in the service area to the text sent to Geoapify."""
    addresses: dict[str, str] = {}
    try:
        for record in read_records(csv_path):
            row = parse_row(record)
            if row is not None and is_in_service_area(row.state):
                addresses.setdefault(row.address_key, row.geocoding_text)
    except CsvHeaderError as error:
        raise CommandError(str(error)) from error
    except (OSError, UnicodeDecodeError) as error:
        raise CommandError(f"cannot read the fuel-price CSV {csv_path}: {error}") from error
    return addresses


def is_highway_style(address_key: str) -> bool:
    address = address_key.split("|", 1)[0]
    return HIGHWAY_STYLE_PATTERN.search(address) is not None


def spread(keys: Sequence[str], count: int) -> list[str]:
    """Evenly spaced picks over sorted keys, so the picks cover many states."""
    ordered = sorted(keys)
    if count <= 0:
        return []
    if count >= len(ordered):
        return ordered
    step = len(ordered) / count
    return [ordered[int(index * step)] for index in range(count)]


def pick_sample(keys: Sequence[str], size: int) -> list[str]:
    """A stable sample: half highway-style, half ordinary addresses, each spread out.

    Always picked from the full address set, so `--sample N --resume` keeps the same N.
    """
    highway = [key for key in keys if is_highway_style(key)]
    ordinary = [key for key in keys if not is_highway_style(key)]
    # Half each; if one group is too small, the other one fills the gap.
    highway_count = min(len(highway), size // 2)
    ordinary_count = min(len(ordinary), size - highway_count)
    highway_count = min(len(highway), size - ordinary_count)
    return sorted(spread(highway, highway_count) + spread(ordinary, ordinary_count))


def select_todo(*, all_keys: Sequence[str], sample_size: int | None, skip: set[str]) -> list[str]:
    """Sorted keys to submit: the sample (or everything) minus done and in-progress keys."""
    chosen = pick_sample(all_keys, sample_size) if sample_size is not None else all_keys
    return sorted(key for key in chosen if key not in skip)


def positive_int(text: str) -> int:
    """argparse type for --sample and --max-credits: a whole number of at least 1."""
    if not (text.isascii() and text.isdecimal()) or int(text) < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number of at least 1, got {text!r}")
    return int(text)


def read_done_keys(out_path: Path) -> set[str]:
    """Address keys already saved in the output file (empty if there is no file)."""
    if not out_path.exists():
        return set()
    try:
        with out_path.open(newline="", encoding="utf-8") as handle:
            content = handle.read()
    except (OSError, UnicodeDecodeError) as error:
        raise CommandError(f"cannot read the output file {out_path}: {error}") from error
    # Every row this command writes ends with a newline; a missing one means a cut-off row.
    if content and not content.endswith("\n"):
        raise CommandError(
            f"{out_path} does not end with a newline, so its last row may be cut off; "
            "remove that row, then rerun with --resume"
        )
    reader = csv.DictReader(io.StringIO(content, newline=""))
    if reader.fieldnames != OUTPUT_COLUMNS:
        raise CommandError(
            f"{out_path} does not have the expected header {','.join(OUTPUT_COLUMNS)}"
        )
    rows = list(reader)
    # DictReader marks a row with too many fields with a None key, too few with None values.
    if any(None in row or None in row.values() for row in rows):
        raise CommandError(f"{out_path} has a row with the wrong number of columns")
    return {row["address_key"] for row in rows}


class Command(BaseCommand):
    help = "Owner only: geocode unique station addresses with Geoapify (uses API credits)."
    # Injected by tests only (a fake transport and a no-wait sleep).
    stealth_options = ("transport", "client_options")

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--sample", type=positive_int, help="Geocode only a stable sample of N addresses."
        )
        parser.add_argument(
            "--resume",
            action="store_true",
            help="Fetch pending jobs, then skip addresses already in --out.",
        )
        parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output CSV path.")
        parser.add_argument(
            "--max-credits",
            type=positive_int,
            default=DEFAULT_MAX_CREDITS,
            help="Stop before this run's estimated credits pass N (free plan: 3,000/day).",
        )

    def handle(self, *args: object, **options: object) -> None:
        self.out_path: Path = options["out"]
        # Before any request: once a job is paid for, its id must be writable next to --out.
        try:
            self.out_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise CommandError(f"cannot create the folder for {self.out_path}: {error}") from None
        with exclusive_run_lock(self.out_path):
            self._geocode(options)

    def _geocode(self, options: dict[str, object]) -> None:
        self.pending = PendingJobsFile(pending_jobs_path(self.out_path))
        self._refuse_overwrite(resume=bool(options["resume"]))
        self.addresses = collect_addresses(DEFAULT_CSV)
        self.done = read_done_keys(self.out_path)
        self._check_pending_keys()
        self._forget_fully_saved_jobs()
        self.sample_size: int | None = options["sample"]
        if not self.pending.jobs and not self._todo():
            self.stdout.write("Nothing to geocode: every address is already in the output file.")
            return

        self.client = self._make_client(options)
        self.budget = CreditBudget(limit=options["max_credits"])
        self.result_types: Counter[str] = Counter()
        self.unmatched = 0
        try:
            self._fetch_pending_jobs()
            self._submit_new_jobs(self._todo())
        except RunStopped as stop:
            self.stdout.write(str(stop))
        self._print_summary()

    def _refuse_overwrite(self, *, resume: bool) -> None:
        for path in (self.out_path, self.pending.path):
            if path.exists() and not resume:
                raise CommandError(
                    f"{path} exists; pass --resume to add to it instead of redoing it"
                )

    def _check_pending_keys(self) -> None:
        unknown = self.pending.in_progress_keys() - self.addresses.keys()
        if unknown:
            raise CommandError(
                f"{self.pending.path} names {len(unknown)} address keys that are not in the CSV"
            )

    def _forget_fully_saved_jobs(self) -> None:
        """A run that stopped after appending a job but before forgetting it needs no request."""
        for job_id, keys in list(self.pending.jobs.items()):
            if self.done.issuperset(keys):
                self.pending.remove(job_id)
                self.stdout.write(
                    f"Job {job_id} is already saved; removed it from the pending list."
                )

    def _todo(self) -> list[str]:
        skip = self.done | self.pending.in_progress_keys()
        return select_todo(all_keys=sorted(self.addresses), sample_size=self.sample_size, skip=skip)

    def _make_client(self, options: dict[str, object]) -> GeoapifyBatchClient:
        transport: Transport = options.get("transport") or HttpClientTransport()
        try:
            return GeoapifyBatchClient(
                transport=transport,
                api_key=settings.GEOAPIFY_API_KEY,
                base_url=settings.GEOAPIFY_BASE_URL,
                connect_timeout_s=settings.GEOAPIFY_CONNECT_TIMEOUT_S,
                total_timeout_s=settings.GEOAPIFY_TOTAL_TIMEOUT_S,
                **options.get("client_options", {}),
            )
        except GeocodingError as error:
            raise CommandError(str(error)) from error

    def _fetch_pending_jobs(self) -> None:
        """Jobs from an earlier run are already paid for: fetch them, never resubmit."""
        for job_id, keys in list(self.pending.jobs.items()):
            self._collect(job_id=job_id, keys=keys)

    def _submit_new_jobs(self, todo: list[str]) -> None:
        for start in range(0, len(todo), MAX_BATCH_SIZE):
            batch = todo[start : start + MAX_BATCH_SIZE]
            self._stop_if_over_budget(estimate=new_job_estimate(len(batch)))
            try:
                job_id = self.client.submit([self.addresses[key] for key in batch])
            except GeocodingError as error:
                raise CommandError(
                    f"{error} (finished batches are saved; a job may have been created; check "
                    "the Geoapify dashboard before rerunning with --resume)"
                ) from error
            # Shown first, so the paid job id is visible even if saving it fails below.
            self.stdout.write(f"Created job {job_id} for {len(batch)} addresses.")
            self.stdout.flush()
            # Saved before any waiting, so a crash or Ctrl-C cannot lose a paid job.
            self.pending.add(job_id=job_id, keys=batch)
            self.budget.charge(CREDITS_PER_ADDRESS * len(batch) + CREDITS_PER_REQUEST)
            self._collect(job_id=job_id, keys=batch)
            self.stdout.write(f"Processed {start + len(batch)} of {len(todo)} addresses.")

    def _stop_if_over_budget(self, *, estimate: Decimal) -> None:
        """Stop before a request that could pass --max-credits; saved results stay."""
        if not self.budget.allows(estimate):
            raise RunStopped(
                f"Credit budget reached: the next job needs about {estimate} credits and "
                f"{self.budget.spent} of {self.budget.limit} are used. {self._left_message()}"
            )

    def _left_message(self) -> str:
        waiting = self.pending.in_progress_keys() - self.done
        remaining = len(self._todo()) + len(waiting)
        return f"Addresses left: {remaining}; rerun with --resume tomorrow."

    def _collect(self, *, job_id: str, keys: Sequence[str]) -> None:
        """Wait for one job, append its rows, then forget it in the pending-jobs file."""
        texts = [self.addresses[key] for key in keys]
        kept = f"job {job_id} is kept in {self.pending.path}"
        # Each poll is 1 credit (Q-13), so the budget caps how many may be made.
        poll_limit = self.budget.requests_left()
        is_capped_by_budget = poll_limit < self.client.max_polls
        try:
            fetched = (
                self.client.fetch(job_id, address_count=len(keys), poll_limit=poll_limit)
                if poll_limit > 0
                else JobFetch(items=None, polls=0)
            )
            self.budget.charge(CREDITS_PER_REQUEST * fetched.polls)
            if fetched.items is None and is_capped_by_budget:
                raise RunStopped(
                    f"Credit budget reached while waiting for job {job_id} (kept in "
                    f"{self.pending.path}); {self.budget.spent} of {self.budget.limit} are "
                    f"used. {self._left_message()}"
                )
            if fetched.items is None:
                raise RunStopped(
                    f"Geoapify is still processing job {job_id} (kept in {self.pending.path}); "
                    "rerun with --resume later."
                )
            outcome = match_results(texts=texts, items=fetched.items)
        except GeocodingError as error:
            raise CommandError(
                f"{error} ({kept}; rerun with --resume, or remove it from that file "
                "to submit its addresses again)"
            ) from error
        key_by_text = {self.addresses[key]: key for key in keys}
        # Skip keys already saved, in case an earlier run stopped between append and forget.
        new_results = [
            result for result in outcome.results if key_by_text[result.query_text] not in self.done
        ]
        self._append(key_by_text=key_by_text, results=new_results)
        self.done.update(key_by_text[result.query_text] for result in new_results)
        self.pending.remove(job_id)
        self.result_types.update(result.result_type or "no result" for result in new_results)
        # Unmatched addresses are not saved, so a later --resume asks for them again.
        self.unmatched += len(outcome.unmatched_texts)

    def _append(self, *, key_by_text: dict[str, str], results: Sequence[GeocodeResult]) -> None:
        """Append one job's rows right away, so a failed later job loses no paid results."""
        is_new = not self.out_path.exists()
        with self.out_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if is_new:
                writer.writerow(OUTPUT_COLUMNS)
            writer.writerows(_output_row(key_by_text[r.query_text], r) for r in results)
            # On disk before the job is forgotten, so a power cut cannot lose paid rows.
            handle.flush()
            os.fsync(handle.fileno())

    def _print_summary(self) -> None:
        summary = ", ".join(f"{name} {count}" for name, count in self.result_types.most_common())
        self.stdout.write(f"Result types: {summary or 'none'}")
        self.stdout.write(f"Not in the reply (rerun with --resume): unmatched {self.unmatched}")
        self.stdout.write(
            f"Estimated credits used: {self.budget.spent} of {self.budget.limit} "
            "(1 credit per poll assumed)"
        )


def _output_row(address_key: str, result: GeocodeResult) -> list[str]:
    return [
        address_key,
        result.lat,
        result.lon,
        result.result_type,
        result.confidence,
        result.state_code,
        SOURCE,
        result.attribution,
        result.license,
        result.source_url,
    ]
