"""The API edge: `POST /api/v1/fuel-plans`, the map page and the JSON 404 for other paths.

The POST view only checks and parses the request, hands a validated `PlanRequest` and a
fresh `CallBudget` to the planner, and adds the per-response `map_url` and `meta`.
Every error it raises becomes the §7a envelope through `errors.api_exception_handler`.
"""

import json
import logging
from collections.abc import Mapping
from typing import BinaryIO
from uuid import UUID

from django.core.cache import caches
from django.db import DatabaseError
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from rest_framework.exceptions import ParseError
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.parsers import JSONParser
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, BaseThrottle
from rest_framework.views import APIView

from .errors import (
    BODY_KEY,
    InvalidRequest,
    error_response,
    internal_error_response,
    not_found_response,
)
from .map_page import PlanShapeError, build_map_page
from .parsing import whole_number
from .planner import build_trip_planner, planobj_cache_key
from .routing import CALL_LIMIT, CallBudget
from .serializers import parse_plan_request, response_body

logger = logging.getLogger(__name__)

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


class FuelPlanView(APIView):
    """`POST /api/v1/fuel-plans`: size check → JSON parse → strict schema → planner."""

    http_method_names = ["post"]  # also makes OPTIONS a 405 with `Allow: POST`
    parser_classes = [StrictJsonParser]
    renderer_classes = [JSONRenderer]
    content_negotiation_class = JsonOnlyNegotiation
    throttle_classes = [AnonRateThrottle]  # this view only: other pages never use its bucket

    def get_throttles(self) -> list[BaseThrottle]:
        """Only POSTs count: a GET or OPTIONS (a 405) must not use up the 30/min bucket."""
        if self.request.method != "POST":
            return []
        return super().get_throttles()

    def post(self, request: Request) -> Response:
        require_small_body(request.META)
        plan_request = parse_plan_request(request.data)
        outcome = build_trip_planner().plan(
            plan_request=plan_request, budget=CallBudget(CALL_LIMIT)
        )
        # Built per response from this request's Host (checked by ALLOWED_HOSTS), never saved.
        map_url = request.build_absolute_uri(
            reverse("fuelplan:fuel-plan-map", kwargs={"plan_id": outcome.plan_id})
        )
        return Response(
            response_body(saved=outcome.body, map_url=map_url, meta=outcome.meta.as_json())
        )


MAP_METHODS = ["GET", "HEAD"]
# Sends the page's origin as Referer to the OSM tile server, which its tile policy asks for
# (spec 02 NFR-6); Django's default `same-origin` would send nothing to another site.
MAP_REFERRER_POLICY = "strict-origin-when-cross-origin"


@csrf_exempt  # read-only; a POST here must get the JSON 405, not a CSRF 403
def fuel_plan_map(request: HttpRequest, plan_id: UUID) -> HttpResponse:
    """The map page of a saved plan (spec 02): one cache read, 0 routing calls, no writes."""
    if request.method not in MAP_METHODS:
        return error_response(
            code="method_not_allowed",
            message=f"Use {' or '.join(MAP_METHODS)} for this endpoint.",
            details={"allowed": list(MAP_METHODS)},
            status=405,
            headers={"Allow": ", ".join(MAP_METHODS)},
        )
    try:
        saved_plan = caches["default"].get(planobj_cache_key(str(plan_id)))
        if saved_plan is None:  # unknown, expired or culled: 01's 404, never `raise Http404`
            return plan_not_found_response(plan_id)
        page = build_map_page(saved_plan)
    except (DatabaseError, PlanShapeError):
        # Caught here, not left to handler500, so even DEBUG on never shows a traceback page.
        logger.exception("Map page for plan %s could not be built", plan_id)
        return internal_error_response()
    # Without the request: no context processor runs, so no session, CSRF cookie or query.
    html = render_to_string("fuelplan/map.html", {"page": page})
    return HttpResponse(html, headers={"Referrer-Policy": MAP_REFERRER_POLICY})


def plan_not_found_response(plan_id: UUID) -> JsonResponse:
    """01's 404 for a canonical plan id with no saved plan (spec 02 FR-9)."""
    return error_response(
        code="plan_not_found",
        message="No saved plan with this id; it may have expired. Send the trip again.",
        details={"plan_id": str(plan_id)},
        status=404,
    )


@csrf_exempt  # any method may arrive here; it must get the JSON 404, not a CSRF 403
def api_not_found(request: HttpRequest) -> JsonResponse:
    """The JSON 404 for every other `/api/v1/` path, also with DEBUG on (owner, 2026-10-28)."""
    return not_found_response()
