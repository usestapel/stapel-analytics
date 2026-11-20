"""The JSON bodies the committed schema declares are the bodies the views send.

An ``@extend_schema`` annotation is a claim the generator cannot check: the
funnel listing was declared as ONE funnel while it answers an array. This
gate performs each JSON operation of ``docs/schema.v2.json`` and validates
the body against the schema it promised. A JSON operation with no recipe
below fails rather than being skipped.
"""
import copy
import json
from pathlib import Path

import jsonschema
import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

SCHEMA = json.loads(
    (Path(__file__).resolve().parent.parent / "docs" / "schema.v2.json").read_text()
)
V1 = "/analytics/api/v1"
STEPS = ["step.one", "step.two"]


@pytest.fixture(autouse=True)
def _declared(settings):
    settings.STAPEL_ANALYTICS = {
        "EVENTS": {name: {"description": name, "flow": "f"} for name in STEPS},
    }


def _batch():
    now = int(timezone.now().timestamp() * 1000)
    return {
        "events": [
            {"id": "e1", "kind": "track", "name": STEPS[0], "props": {}, "ts": now},
            {"id": "e2", "kind": "track", "name": "nobody.declared", "ts": now},
            {"kind": "track", "ts": now},
        ],
        "context": {"platform": "android", "app_version": "1.0.0"},
    }


def _funnel(client):
    response = client.post(f"{V1}/funnels", {"slug": "checkout", "steps": STEPS}, format="json")
    assert response.status_code == 201, response.content
    return "checkout"


#: ``(METHOD, path) -> recipe(api_client, staff_client) -> response``.
RECIPES = {
    ("POST", "/analytics/api/events"): lambda anon, staff: anon.post(
        "/analytics/api/events", _batch(), format="json"
    ),
    ("POST", f"{V1}/events"): lambda anon, staff: anon.post(f"{V1}/events", _batch(), format="json"),
    ("GET", f"{V1}/event-registry"): lambda anon, staff: staff.get(f"{V1}/event-registry"),
    ("GET", f"{V1}/funnels"): lambda anon, staff: (
        _funnel(staff), staff.get(f"{V1}/funnels")
    )[1],
    ("POST", f"{V1}/funnels"): lambda anon, staff: staff.post(
        f"{V1}/funnels", {"slug": "signup", "steps": STEPS}, format="json"
    ),
    ("GET", f"{V1}/funnels/{{slug}}"): lambda anon, staff: staff.get(
        f"{V1}/funnels/{_funnel(staff)}"
    ),
    ("PATCH", f"{V1}/funnels/{{slug}}"): lambda anon, staff: staff.patch(
        f"{V1}/funnels/{_funnel(staff)}", {"title": "Checkout"}, format="json"
    ),
    ("GET", f"{V1}/funnels/{{slug}}/report"): lambda anon, staff: staff.get(
        f"{V1}/funnels/{_funnel(staff)}/report"
    ),
    ("GET", f"{V1}/reports/events"): lambda anon, staff: staff.get(f"{V1}/reports/events"),
}


def _json_schema(node):
    if isinstance(node, list):
        return [_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    rebuilt = {k: _json_schema(v) for k, v in node.items() if k != "nullable"}
    if node.get("nullable"):
        return {"anyOf": [rebuilt, {"type": "null"}]}
    return rebuilt


def _operations():
    for path, item in SCHEMA["paths"].items():
        for method, op in item.items():
            if not isinstance(op, dict):
                continue
            for code, response in op.get("responses", {}).items():
                body = response.get("content", {}).get("application/json", {}).get("schema")
                if code.startswith("2") and body is not None:
                    yield method.upper(), path, int(code), body


OPERATIONS = list(_operations())


def test_the_gate_sees_its_subject():
    assert len(OPERATIONS) >= 9, OPERATIONS
    assert {(m, p) for m, p, _c, _b in OPERATIONS} == set(RECIPES)


@pytest.mark.parametrize(
    "method,path,code,body_schema", OPERATIONS, ids=[f"{m} {p}" for m, p, _c, _b in OPERATIONS]
)
def test_the_wire_matches_the_declared_response(api_client, staff_client, method, path, code, body_schema):
    assert (method, path) in RECIPES, f"{method} {path} has a JSON body and no recipe"
    response = RECIPES[(method, path)](api_client, staff_client)
    assert response.status_code == code, response.content
    root = copy.deepcopy(body_schema)
    root["components"] = copy.deepcopy(SCHEMA["components"])
    validator = jsonschema.Draft202012Validator(_json_schema(root))
    body = response.json()
    errors = sorted(validator.iter_errors(body), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"at {list(e.path)}: {e.message}" for e in errors[:10])
    if isinstance(body, list):
        assert body, f"{method} {path}: an empty list validates against any item schema"
