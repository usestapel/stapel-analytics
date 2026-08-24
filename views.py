"""DRF views for stapel-analytics.

Two access models, because there are two audiences.

**Ingest is open by design.** ``AllowAny``, and with an EMPTY authentication
list by default (``INGEST_AUTHENTICATION``). That is not laxity — it is the
only shape that works: the identity of an analytics event is the
``user_hash`` inside the payload, the last batch of a session arrives via
``navigator.sendBeacon`` (which carries no CSRF token, and DRF's
SessionAuthentication enforces CSRF from inside authentication), and the
authorization that matters is the SOURCE write key, not the visitor. A
deployment that wants token-authenticated ingest names its classes in the
setting.

**Everything else is staff-and-owner.** A funnel names business milestones
and a report is the deployment's conversion data; neither is a visitor's to
read.

The gate is ``HasWorkspaceMandateIfScoped`` — the library-shaped mandate
check: in a deployment that can answer the mandate question it enforces the
third principal state (a registered account belonging to no workspace is a
guest, not a user), and in a single-tenant deployment, where no mandate
exists for anybody to hold, it admits. The strict class would 503 everyone in
the second shape, and analytics must be installable there. The scope on top
of it is OWNERSHIP: a caller sees the funnels they authored plus the ones the
project spec declares; staff see all.

Presenter-canonical (§55): a view resolves its presenter through
``get_presenter`` and returns ``StapelResponse(Serializer(presenter.present(
...)))`` — it never instantiates a ``dto.py`` dataclass itself (SWAP002)
and never imports the concrete presenter class (SWAP001).
"""
from __future__ import annotations

import functools

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions
from rest_framework.views import APIView
from stapel_core.django.api.errors import StapelErrorResponse, StapelResponse
from stapel_core.django.api.permissions import (
    HasWorkspaceMandateIfScoped,
    IsStaffUser,
)

from . import services
from .conf import analytics_settings
from .errors import (
    BATCH_KEYS,
    ERR_400_BATCH_SHAPE,
    ERR_400_REPORT_PERIOD,
    ERR_403_FORBIDDEN,
    ERR_404_FUNNEL,
    ERR_409_FUNNEL_DECLARED,
    ERR_413_BODY_TOO_LARGE,
)
from .funnels import UnknownFunnel, list_funnels, report, resolve_funnel
from .ingest import IngestRefused
from .models import Funnel
from .presenters import (
    get_funnel_presenter,
    get_receipt_presenter,
    get_registry_presenter,
    get_report_presenter,
    present_funnel_spec,
)
from .serializers import (
    EventRegistrySerializer,
    EventsReportQuerySerializer,
    EventsReportSerializer,
    FunnelCreateSerializer,
    FunnelDefinitionSerializer,
    FunnelListQuerySerializer,
    FunnelPatchSerializer,
    FunnelReportSerializer,
    FunnelSerializer,
    IngestReceiptSerializer,
    IngestSerializer,
    ReportQuerySerializer,
)


class SerializerSeamMixin:
    """Overridable serializer seam for every stapel-analytics APIView.

    Host projects swap the request/response serializer of any view by
    subclassing and setting ``request_serializer_class`` /
    ``response_serializer_class`` (or overriding the getters for
    per-request decisions) — no need to rewrite the method bodies.
    """

    request_serializer_class = None
    response_serializer_class = None

    def get_request_serializer_class(self):
        return self.request_serializer_class

    def get_response_serializer_class(self):
        return self.response_serializer_class


def _maps_analytics_errors(method):
    """Translate service refusals into the unified error envelope."""

    @functools.wraps(method)
    def wrapper(self, request, *args, **kwargs):
        try:
            return method(self, request, *args, **kwargs)
        except services.AnalyticsError as exc:
            return StapelErrorResponse(exc.status, exc.error_key, exc.params)
        except UnknownFunnel:
            return StapelErrorResponse(404, ERR_404_FUNNEL)

    return wrapper


def _is_staff(request) -> bool:
    user = getattr(request, "user", None)
    return bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))


def _owner_id(request):
    return getattr(getattr(request, "user", None), "pk", None)


def _page_size(requested) -> int:
    """The listing cap, honouring a smaller request.

    A cap that nothing reads is a knob whose deployment value changes
    nothing (``stapel-config-lint`` CFG006), so the funnel listing applies
    it — and a deployment with hundreds of declared funnels gets a bounded
    response instead of the whole table.
    """
    cap = int(analytics_settings.MAX_PAGE_SIZE or 100)
    if not requested:
        return cap
    return max(1, min(int(requested), cap))


def _scoped_funnel(request, slug):
    """``(row, error_response)`` — 404 for a stranger's funnel.

    Not 403: a slug is guessable, and "exists but not yours" turns the
    endpoint into an oracle for which funnels another tenant runs — which is
    a description of their product roadmap.
    """
    row = Funnel.objects.filter(slug=slug).first()
    if row is None:
        from .funnels import declared_funnels

        if slug in declared_funnels():
            # It exists and is real, but its home is the project spec.
            return None, StapelErrorResponse(409, ERR_409_FUNNEL_DECLARED)
        return None, StapelErrorResponse(404, ERR_404_FUNNEL)
    if _is_staff(request):
        return row, None
    if row.owner_id and str(row.owner_id) == str(_owner_id(request)):
        return row, None
    if row.owner_id is None:
        # An unowned funnel (created in code, or by an erased account) is an
        # operator's object, never a user's.
        return None, StapelErrorResponse(403, ERR_403_FORBIDDEN)
    return None, StapelErrorResponse(404, ERR_404_FUNNEL)


@extend_schema(tags=["Analytics"])
class IngestView(SerializerSeamMixin, APIView):
    """``POST`` a batch of events — the collector endpoint of the facade.

    Answers **202** with a receipt, even when some events were refused: the
    batch WAS processed, and a 4xx would make the facade retry the same
    twenty events until its ladder gives up and drops all of them. The
    receipt says exactly what happened to each one.
    """

    permission_classes = [permissions.AllowAny]
    request_serializer_class = IngestSerializer
    response_serializer_class = IngestReceiptSerializer

    def get_authenticators(self):
        """Resolve ``INGEST_AUTHENTICATION`` — empty by default.

        Per-view rather than per-project because this is the ONE endpoint in
        the module that a browser reaches without a session, and the whole
        reason it works from ``sendBeacon`` is that no authenticator runs a
        CSRF check on it.
        """
        from django.utils.module_loading import import_string

        classes = analytics_settings.INGEST_AUTHENTICATION or []
        resolved = []
        for entry in classes:
            cls = import_string(entry) if isinstance(entry, str) else entry
            resolved.append(cls())
        return resolved

    @extend_schema(request=IngestSerializer, responses={202: IngestReceiptSerializer})
    def post(self, request):
        max_body = int(analytics_settings.MAX_BODY_BYTES or 0)
        length = request.META.get("CONTENT_LENGTH")
        if max_body and length and str(length).isdigit() and int(length) > max_body:
            return StapelErrorResponse(
                413, ERR_413_BODY_TOO_LARGE, {"max": max_body}
            )
        body = request.data
        if not isinstance(body, dict):
            return StapelErrorResponse(400, ERR_400_BATCH_SHAPE)
        try:
            result = services.record_batch(body)
        except IngestRefused as exc:
            key = BATCH_KEYS.get(exc.error_key, ERR_400_BATCH_SHAPE)
            return StapelErrorResponse(exc.status, key, exc.params)
        response_cls = self.get_response_serializer_class()
        receipt = get_receipt_presenter()().present(result)
        return StapelResponse(response_cls(receipt), status=202)


@extend_schema(tags=["Analytics"])
class EventRegistryView(SerializerSeamMixin, APIView):
    """What this deployment declares, how it enforces it, and what mirrors it.

    Readable by any authenticated caller: an app-layer developer needs the
    vocabulary to fire against it, and the registry names events, never
    people.
    """

    permission_classes = [HasWorkspaceMandateIfScoped]
    response_serializer_class = EventRegistrySerializer

    @extend_schema(responses={200: EventRegistrySerializer})
    def get(self, request):
        response_cls = self.get_response_serializer_class()
        return StapelResponse(response_cls(get_registry_presenter()().present()))


_MINE_PARAM = OpenApiParameter(
    name="mine", type=bool, location=OpenApiParameter.QUERY, required=False
)


@extend_schema(tags=["Analytics"])
class FunnelListCreateView(SerializerSeamMixin, APIView):
    """List funnels (authored + declared), or author a new one."""

    permission_classes = [HasWorkspaceMandateIfScoped]
    request_serializer_class = FunnelCreateSerializer
    response_serializer_class = FunnelDefinitionSerializer

    @extend_schema(parameters=[_MINE_PARAM], responses={200: FunnelDefinitionSerializer})
    def get(self, request):
        query = FunnelListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        owner_id = None
        if params.get("mine") or not _is_staff(request):
            owner_id = _owner_id(request)
        specs = list_funnels(
            owner_id=owner_id, workspace_id=params.get("workspace_id")
        )[: _page_size(params.get("limit"))]
        response_cls = self.get_response_serializer_class()
        return StapelResponse(
            response_cls([present_funnel_spec(spec) for spec in specs], many=True)
        )

    @extend_schema(request=FunnelCreateSerializer, responses={201: FunnelSerializer})
    @_maps_analytics_errors
    def post(self, request):
        request_cls = self.get_request_serializer_class()
        payload = request_cls(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        row = services.save_funnel(
            slug=data["slug"],
            steps=data["steps"],
            title=data.get("title", ""),
            description=data.get("description", ""),
            window_seconds=data.get("window_seconds"),
            is_active=data.get("is_active", True),
            owner_id=_owner_id(request),
            workspace_id=data.get("workspace_id"),
        )
        return StapelResponse(
            FunnelSerializer(get_funnel_presenter()().present(row)), status=201
        )


@extend_schema(tags=["Analytics"])
class FunnelDetailView(SerializerSeamMixin, APIView):
    """Read, re-author or delete one funnel."""

    permission_classes = [HasWorkspaceMandateIfScoped]
    request_serializer_class = FunnelPatchSerializer
    response_serializer_class = FunnelSerializer

    @extend_schema(responses={200: FunnelSerializer})
    def get(self, request, slug):
        row, error = _scoped_funnel(request, slug)
        if error is not None:
            # A declared funnel has no row but is perfectly readable: its
            # definition is public to anyone who may read funnels at all.
            from .funnels import declared_funnels

            spec = declared_funnels().get(slug)
            if spec is not None:
                return StapelResponse(
                    FunnelDefinitionSerializer(present_funnel_spec(spec))
                )
            return error
        return StapelResponse(
            self.get_response_serializer_class()(get_funnel_presenter()().present(row))
        )

    @extend_schema(request=FunnelPatchSerializer, responses={200: FunnelSerializer})
    @_maps_analytics_errors
    def patch(self, request, slug):
        row, error = _scoped_funnel(request, slug)
        if error is not None:
            return error
        payload = self.get_request_serializer_class()(data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        # The WHOLE rule is re-validated, not only what changed: a step name
        # that was legal last month must not survive a registry change
        # nobody re-checked.
        row = services.save_funnel(
            slug=row.slug,
            steps=data.get("steps", row.steps),
            title=data.get("title", row.title),
            description=data.get("description", row.description),
            window_seconds=data.get("window_seconds", row.window_seconds),
            is_active=data.get("is_active", row.is_active),
            instance=row,
        )
        return StapelResponse(
            self.get_response_serializer_class()(get_funnel_presenter()().present(row))
        )

    @extend_schema(responses={204: None})
    def delete(self, request, slug):
        row, error = _scoped_funnel(request, slug)
        if error is not None:
            return error
        services.delete_funnel(row)
        return StapelResponse(status=204)


@extend_schema(tags=["Analytics"])
class FunnelReportView(SerializerSeamMixin, APIView):
    """Conversion by step over a period, optionally versus the period before."""

    permission_classes = [HasWorkspaceMandateIfScoped]
    response_serializer_class = FunnelReportSerializer

    @extend_schema(responses={200: FunnelReportSerializer})
    @_maps_analytics_errors
    def get(self, request, slug):
        row, error = _scoped_funnel(request, slug)
        if error is not None and error.status_code != 409:
            return error
        query = ReportQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        spec = resolve_funnel(slug)
        try:
            computed = report(
                spec,
                start=params.get("start"),
                end=params.get("end"),
                compare=bool(params.get("compare")),
            )
        except ValueError:
            return StapelErrorResponse(400, ERR_400_REPORT_PERIOD)
        return StapelResponse(
            self.get_response_serializer_class()(get_report_presenter()().present(computed))
        )


@extend_schema(tags=["Analytics"])
class EventsReportView(SerializerSeamMixin, APIView):
    """Counts grouped by ``name`` / ``source`` / ``kind`` over a period.

    Staff-only: unlike a funnel (a definition someone authored), this is the
    raw shape of the deployment's traffic.
    """

    permission_classes = [IsStaffUser]
    response_serializer_class = EventsReportSerializer

    @extend_schema(responses={200: EventsReportSerializer})
    def get(self, request):
        from datetime import timedelta

        from django.utils import timezone

        from .store import rollup

        query = EventsReportQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        end = params.get("end") or timezone.now()
        start = params.get("start") or end - timedelta(days=7)
        if start >= end:
            return StapelErrorResponse(400, ERR_400_REPORT_PERIOD)
        group_by = params.get("group_by") or ["name"]
        rows = rollup(group_by=group_by, time_range=(start, end))
        report_dto = get_report_presenter()().present_events(
            rows, group_by=group_by, start=start, end=end
        )
        return StapelResponse(self.get_response_serializer_class()(report_dto))


__all__ = [
    "EventRegistryView",
    "EventsReportView",
    "FunnelDetailView",
    "FunnelListCreateView",
    "FunnelReportView",
    "IngestView",
    "SerializerSeamMixin",
]
