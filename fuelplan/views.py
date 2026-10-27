"""The API edge: `POST /api/v1/fuel-plans` and the JSON 404 for other `/api/v1/` paths.

The view only checks and parses the request, then hands a validated `PlanRequest`
and a fresh `CallBudget` to the planner. Every error it raises becomes the §7a
envelope through `errors.api_exception_handler`.
"""

import json
from collections.abc import Mapping
from typing import BinaryIO

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework.exceptions import ParseError
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.parsers import JSONParser
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from .errors import BODY_KEY, InvalidRequest, not_found_response
from .parsing import whole_number
from .routing import CALL_LIMIT, CallBudget
from .serializers import PlanRequest, parse_plan_request

MAX_BODY_BYTES = 10_240  # NFR-10, checked before the body is read
# Django and DRF read Content-Length with int(), which fails beyond 4,300 digits (even
# leading zeros); they then treat the body as empty. Longer header text is refused here.
CONTENT_LENGTH_MAX_CHARS = 20


class JsonOnlyNegotiation(DefaultContentNegotiation):
    """Parsers are still matched to Content-Type (→ 415), but Accept is ignored.

    With JSON as the only renderer, DRF would answer `Accept: text/html` with a 406,
    which is not in the §7a catalogue; JSON is always the answer instead.
    """

    def select_renderer(
        self, request: Request, renderers: list[BaseRenderer], format_suffix: str | None = None
    ) -> tuple[BaseRenderer, str]:
        renderer = renderers[0]
        return renderer, renderer.media_type


class StrictJsonParser(JSONParser):
    """UTF-8 JSON without NaN/Infinity; failures give one plain reason, not Python text."""

    def parse(
        self,
        stream: BinaryIO,
        media_type: str | None = None,
        parser_context: Mapping[str, object] | None = None,
    ) -> object:
        try:
            text = stream.read().decode("utf-8")
        except UnicodeDecodeError:
            raise ParseError("Body is not valid UTF-8.") from None
        try:
            return json.loads(text, parse_constant=_refuse_constant)
        except _NonFiniteNumber:
            raise ParseError("NaN and Infinity are not allowed in JSON numbers.") from None
        except (ValueError, RecursionError):
            raise ParseError("Body is not valid JSON.") from None


class _NonFiniteNumber(ValueError):
    """The body used NaN, Infinity or -Infinity, which strict JSON does not have."""


def _refuse_constant(name: str) -> float:
    raise _NonFiniteNumber(name)


def require_small_body(meta: Mapping[str, object]) -> None:
    """400 `_body` unless Content-Length is a whole number from 1 to 10,240 (FR-34).

    Runs before the body is read. Content-Length is our 10 KiB cap; Django's
    DATA_UPLOAD_MAX_MEMORY_SIZE (via request.body) is a looser backstop.
    """
    raw = str(meta.get("CONTENT_LENGTH") or "").strip()
    length = whole_number(raw, at_most=MAX_BODY_BYTES + 1)
    if not raw:
        reason = "The body is empty or has no Content-Length header; send a JSON object."
    elif length is None:
        reason = "Content-Length must be a whole number of bytes."
    elif length == 0:
        reason = "The body is empty; send a JSON object."
    elif length > MAX_BODY_BYTES:
        reason = f"The body is larger than {MAX_BODY_BYTES:,} bytes."
    elif len(raw) > CONTENT_LENGTH_MAX_CHARS:  # a small number padded with zeros
        reason = "Content-Length must be a whole number of bytes."
    else:
        return
    raise InvalidRequest({BODY_KEY: [reason]})


def plan_trip(*, plan_request: PlanRequest, budget: CallBudget) -> Response:
    """TODO(2C): replace with the planner (resolve → caches → route → optimise → body).

    Until build step 2C lands, a valid request ends here as a logged 500.
    """
    raise NotImplementedError("TODO(2C): the planner is not built yet")


class FuelPlanView(APIView):
    """`POST /api/v1/fuel-plans`: size check → JSON parse → strict schema → planner."""

    http_method_names = ["post"]  # also makes OPTIONS a 405 with `Allow: POST`
    parser_classes = [StrictJsonParser]
    renderer_classes = [JSONRenderer]
    content_negotiation_class = JsonOnlyNegotiation
    throttle_classes = [AnonRateThrottle]  # this view only: other pages never use its bucket

    def post(self, request: Request) -> Response:
        require_small_body(request.META)
        plan_request = parse_plan_request(request.data)
        return plan_trip(plan_request=plan_request, budget=CallBudget(CALL_LIMIT))


@csrf_exempt  # any method may arrive here; it must get the JSON 404, not a CSRF 403
def api_not_found(request: HttpRequest) -> JsonResponse:
    """The JSON 404 for every other `/api/v1/` path, also with DEBUG on (owner, 2026-10-28)."""
    return not_found_response()
