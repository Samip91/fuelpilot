"""Spec 01 TC-38, TC-39, TC-40: platform pins, offline tests, secure defaults."""

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from unittest import skipUnless

import django
from django.conf import settings

from .base import NetworkAccessBlocked, OfflineTestCase

REPO = Path(settings.BASE_DIR)


def _manage(*args: str, env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    """Run `manage.py` in a clean environment without the developer's FUELPILOT_/DJANGO_ values.

    `load_dotenv` never overrides a variable that is already set, so a value of ""
    here stands for "unset" without the developer's `.env` filling it in.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DJANGO_", "FUELPILOT_"))
    }
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "manage.py", *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class PlatformTests(OfflineTestCase):
    def test_django_and_dependencies_are_pinned_to_the_confirmed_versions(self) -> None:
        pins = (REPO / "requirements.txt").read_text(encoding="utf-8").split()

        self.assertEqual(django.VERSION[:3], (6, 1, 2))
        self.assertEqual(
            pins, ["Django==6.1.2", "djangorestframework==3.18.3", "python-dotenv==1.2.4"]
        )
        self.assertGreaterEqual(sys.version_info[:2], (3, 12))

    def test_tests_cannot_open_network_connections(self) -> None:
        with self.assertRaises(NetworkAccessBlocked), socket.socket() as sock:
            sock.connect(("1.1.1.1", 443))


def _is_git_checkout() -> bool:
    if shutil.which("git") is None:
        return False
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"], cwd=REPO, capture_output=True, check=False
    )
    return probe.returncode == 0


SECRET = {"DJANGO_SECRET_KEY": "x" * 50}
PRINT_DEBUG = (
    "shell",
    "--no-imports",
    "-c",
    "from django.conf import settings; print(settings.DEBUG)",
)

PRINT_GEOAPIFY = (
    "shell",
    "--no-imports",
    "-c",
    "from django.conf import settings as s; print(s.GEOAPIFY_BASE_URL,"
    " s.GEOAPIFY_CONNECT_TIMEOUT_S, s.GEOAPIFY_TOTAL_TIMEOUT_S)",
)


class SecureDefaultsTests(OfflineTestCase):
    def test_debug_is_off_when_the_flag_is_unset(self) -> None:
        result = _manage(*PRINT_DEBUG, env_overrides={**SECRET, "FUELPILOT_DEBUG": ""})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")

    def test_debug_is_on_only_when_explicitly_enabled(self) -> None:
        result = _manage(*PRINT_DEBUG, env_overrides={**SECRET, "FUELPILOT_DEBUG": "true"})

        self.assertEqual(result.stdout.strip(), "True", result.stderr)

    def test_startup_refuses_without_a_secret_key_when_debug_is_off(self) -> None:
        result = _manage(
            "check", env_overrides={"DJANGO_SECRET_KEY": "", "FUELPILOT_DEBUG": "false"}
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY is required", result.stderr)

    def test_a_plan_ttl_that_is_not_a_positive_whole_number_stops_startup(self) -> None:
        for value in ("abc", "0", "-5", "1.5"):
            with self.subTest(ttl=value):
                result = _manage("check", env_overrides={**SECRET, "FUELPILOT_PLAN_TTL_S": value})

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("FUELPILOT_PLAN_TTL_S must be a whole number", result.stderr)

    def test_env_example_holds_empty_secrets(self) -> None:
        example = (REPO / ".env.example").read_text(encoding="utf-8")

        self.assertIn("DJANGO_SECRET_KEY=\n", example)
        self.assertIn("GEOAPIFY_API_KEY=\n", example)

    def test_geoapify_endpoint_settings_default_and_clamp_like_the_routing_ones(self) -> None:
        cases = {
            # (base URL, connect, total) env values -> printed settings
            ("", "", ""): "https://api.geoapify.com 10 30",
            ("http://geo.test:8080/", "0", "999"): "http://geo.test:8080 1 60",
            ("", "abc", "-5"): "https://api.geoapify.com 10 30",
            ("", "60", "1"): "https://api.geoapify.com 60 1",
        }
        for (base_url, connect, total), expected in cases.items():
            # "" stands for unset, so a developer's .env cannot fill it in.
            env = {
                **SECRET,
                "GEOAPIFY_BASE_URL": base_url,
                "GEOAPIFY_CONNECT_TIMEOUT_S": connect,
                "GEOAPIFY_TOTAL_TIMEOUT_S": total,
            }
            with self.subTest(env=(base_url, connect, total)):
                result = _manage(*PRINT_GEOAPIFY, env_overrides=env)

                self.assertEqual(result.stdout.strip(), expected, result.stderr)

    def test_a_timeout_with_a_non_ascii_digit_falls_back_to_the_default(self) -> None:
        # "²".isdigit() is True but int("²") raises, which would crash startup.
        env = {**SECRET, "GEOAPIFY_CONNECT_TIMEOUT_S": "²", "GEOAPIFY_TOTAL_TIMEOUT_S": "٣"}

        result = _manage(*PRINT_GEOAPIFY, env_overrides={**env, "GEOAPIFY_BASE_URL": ""})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "https://api.geoapify.com 10 30")

    @skipUnless(_is_git_checkout(), "needs a git checkout")
    def test_git_ignores_the_env_file(self) -> None:
        result = subprocess.run(
            ["git", "check-ignore", "-q", ".env"], cwd=REPO, capture_output=True, check=False
        )

        self.assertEqual(result.returncode, 0)
