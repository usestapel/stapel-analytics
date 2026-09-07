"""i18n error keys of stapel-analytics.

Only ``error.<status>.analytics_<slug>`` keys are OWNED by this package —
human-readable strings are translations, never literals in responses. The
English registry below is the source; ``translations/errors.<lang>.json``
ships the localized catalogues in the same release (owning keys means
shipping their catalogues).
"""
from stapel_core.django.api.errors import ErrorKeysView, register_service_errors

# ── Ingest (batch-level refusals) ────────────────────────────────────
ERR_400_BATCH_SHAPE = "error.400.analytics_batch_shape"
ERR_400_BATCH_TOO_LARGE = "error.400.analytics_batch_too_large"
ERR_413_BODY_TOO_LARGE = "error.413.analytics_body_too_large"
ERR_401_WRITE_KEY = "error.401.analytics_write_key"

# ── Funnel authoring ─────────────────────────────────────────────────
ERR_400_FUNNEL_STEPS = "error.400.analytics_funnel_steps"
ERR_400_FUNNEL_STEP_UNKNOWN = "error.400.analytics_funnel_step_unknown"
ERR_400_FUNNEL_SLUG_TAKEN = "error.400.analytics_funnel_slug_taken"
ERR_400_REPORT_PERIOD = "error.400.analytics_report_period"
ERR_403_FORBIDDEN = "error.403.analytics_forbidden"
ERR_404_FUNNEL = "error.404.analytics_funnel_not_found"
ERR_409_FUNNEL_DECLARED = "error.409.analytics_funnel_declared"
ERR_409_FUNNEL_CAP = "error.409.analytics_funnel_cap"

# ── The conversion feed ──────────────────────────────────────────────
#: The endpoint is configured and the caller did not present its token, or
#: presented a wrong one. 401 with a ``WWW-Authenticate: Basic`` challenge:
#: the caller is a fetcher that may need to be asked before it sends the
#: password it holds. A feed with NO token configured does not answer this
#: — it 404s, because "wrong token" and "there is no feed here" are
#: different facts and the second one should not be turned into an
#: announcement that a feed exists.
ERR_401_FEED_TOKEN = "error.401.analytics_feed_token"

STAPEL_ANALYTICS_ERRORS = {
    ERR_400_BATCH_SHAPE: "The batch must be an object with an events array",
    ERR_400_BATCH_TOO_LARGE: "A batch carries at most {max} events, this one has {got}",
    ERR_413_BODY_TOO_LARGE: "The request body is larger than {max} bytes",
    ERR_401_WRITE_KEY: "A valid write key is required to send events",
    ERR_400_FUNNEL_STEPS: "A funnel needs between 2 and {max} step names",
    ERR_400_FUNNEL_STEP_UNKNOWN: "No declared event answers to the step {step}",
    ERR_400_FUNNEL_SLUG_TAKEN: "A funnel with the slug {slug} already exists",
    ERR_400_REPORT_PERIOD: "The report period is empty or malformed",
    ERR_403_FORBIDDEN: "You do not have access to this funnel",
    ERR_404_FUNNEL: "Funnel not found",
    ERR_409_FUNNEL_DECLARED: (
        "This funnel is declared in the project spec "
        "(STAPEL_ANALYTICS['FUNNELS']) and is edited there, not over the API"
    ),
    ERR_409_FUNNEL_CAP: "You already have the maximum number of funnels",
    ERR_401_FEED_TOKEN: "A valid feed token is required to read this feed",
}

#: What a client can actually DO about each refusal (core's REMEDIATION_VOCAB).
STAPEL_ANALYTICS_REMEDIATION = {
    ERR_400_BATCH_SHAPE: "fix_input",
    ERR_400_BATCH_TOO_LARGE: "fix_input",
    ERR_413_BODY_TOO_LARGE: "fix_input",
    ERR_401_WRITE_KEY: "reauthenticate",
    ERR_400_FUNNEL_STEPS: "fix_input",
    ERR_400_FUNNEL_STEP_UNKNOWN: "fix_input",
    ERR_400_FUNNEL_SLUG_TAKEN: "fix_input",
    ERR_400_REPORT_PERIOD: "fix_input",
    ERR_403_FORBIDDEN: "contact_support",
    ERR_404_FUNNEL: "verify",
    ERR_409_FUNNEL_DECLARED: "contact_support",
    ERR_409_FUNNEL_CAP: "contact_support",
    ERR_401_FEED_TOKEN: "reauthenticate",
}

register_service_errors(STAPEL_ANALYTICS_ERRORS, remediation=STAPEL_ANALYTICS_REMEDIATION)

#: ``ingest.Rejection.reason`` -> the error key that explains it. Rejections
#: are per-event and travel INSIDE a 202 body rather than as a response
#: status, so they carry keys the same way a refusal does: a client that
#: shows "3 events rejected" without saying why is a client whose user
#: reports "analytics is broken".
REJECTION_KEYS = {
    "pii": "error.400.analytics_event_pii",
    "unregistered": "error.400.analytics_event_unregistered",
    "missing_name": "error.400.analytics_event_missing_name",
    "name_too_long": "error.400.analytics_event_name_too_long",
    "unknown_kind": "error.400.analytics_event_unknown_kind",
    "not_an_object": "error.400.analytics_event_shape",
    "props_not_an_object": "error.400.analytics_event_props_shape",
    "props_not_serializable": "error.400.analytics_event_props_shape",
    "props_too_large": "error.400.analytics_event_props_too_large",
    "invalid_ts": "error.400.analytics_event_invalid_ts",
    "too_old": "error.400.analytics_event_too_old",
}

STAPEL_ANALYTICS_EVENT_ERRORS = {
    "error.400.analytics_event_pii": "A property value looks like personal data",
    "error.400.analytics_event_unregistered": "The event is not in the event registry",
    "error.400.analytics_event_missing_name": "The event carries no name",
    "error.400.analytics_event_name_too_long": "The event name is too long",
    "error.400.analytics_event_unknown_kind": "Unknown event kind",
    "error.400.analytics_event_shape": "The event must be an object",
    "error.400.analytics_event_props_shape": "Event properties must be a JSON object",
    "error.400.analytics_event_props_too_large": "The event properties are too large",
    "error.400.analytics_event_invalid_ts": "The event timestamp is not readable",
    "error.400.analytics_event_too_old": "The event is older than the accepted window",
}

register_service_errors(
    STAPEL_ANALYTICS_EVENT_ERRORS,
    remediation=dict.fromkeys(STAPEL_ANALYTICS_EVENT_ERRORS, "fix_input"),
)

#: Batch-level ``IngestRefused.error_key`` -> the public error key.
BATCH_KEYS = {
    "batch_not_object": ERR_400_BATCH_SHAPE,
    "batch_missing_events": ERR_400_BATCH_SHAPE,
    "batch_events_not_list": ERR_400_BATCH_SHAPE,
    "batch_too_large": ERR_400_BATCH_TOO_LARGE,
    "body_too_large": ERR_413_BODY_TOO_LARGE,
    "write_key": ERR_401_WRITE_KEY,
}


class AnalyticsErrorKeysView(ErrorKeysView):
    """The error-key listing the stapel-translate collector reads.

    Mounted at ``error-keys/`` (the stapel-cdn / workspaces / profiles
    convention). Without it the collector reports this service as having no
    endpoint and its catalogues never get regenerated.
    """

    def get_service_errors(self):
        return {**STAPEL_ANALYTICS_ERRORS, **STAPEL_ANALYTICS_EVENT_ERRORS}


__all__ = (
    [name for name in dir() if name.startswith("ERR_")]
    + [
        "BATCH_KEYS",
        "REJECTION_KEYS",
        "STAPEL_ANALYTICS_ERRORS",
        "STAPEL_ANALYTICS_EVENT_ERRORS",
        "STAPEL_ANALYTICS_REMEDIATION",
        "AnalyticsErrorKeysView",
    ]
)
