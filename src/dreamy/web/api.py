from __future__ import annotations

import json
from collections.abc import Mapping
from urllib.parse import parse_qs, urlparse

from ..read import ReadStore

MAX_ROWS = 200


def _jsonable(value):
    if hasattr(value, "__dataclass_fields__"):
        return {k: _jsonable(getattr(value, k)) for k in value.__dataclass_fields__}
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value


def _int_param(params: dict[str, list[str]], name: str, default: int = 20) -> int:
    try:
        value = int(params.get(name, [default])[0])
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, MAX_ROWS))


def route(store: ReadStore, path: str, query: str = "") -> tuple[int, dict]:
    parsed = urlparse(path)
    route_path = parsed.path
    params = parse_qs(query or parsed.query, keep_blank_values=False)
    if route_path == "/healthz":
        return 200, {"status": "ok", "has_state": True}
    if route_path == "/api/v1/overview":
        latest = store.latest_run()
        return 200, {
            "latest_run": _jsonable(latest),
            "source_stats": _jsonable(store.source_stats()),
            "schedule": _jsonable(store.schedule_state()),
            "total_agent_spend": store.total_agent_spend(),
            "counts": {"projects": len(store.all_projects()), "findings": len(store.findings())},
        }
    if route_path == "/api/v1/runs":
        return 200, {"runs": _jsonable(store.runs_history(_int_param(params, "limit")))}
    if route_path == "/api/v1/findings":
        # Heterogeneous by design: scalar filters bind one string, `state`
        # binds a collection. Annotated explicitly so the mixed value type is
        # a stated contract rather than something mypy infers from whichever
        # branch it sees first.
        filters: dict[str, object] = {}
        for key in ("project_id", "severity", "category"):
            source = "project" if key == "project_id" else key
            if params.get(source):
                filters[key] = params[source][0]
        states = params.get("state")
        if states and states[0] != "all":
            filters["state"] = [s for s in states[0].split(",") if s]
        elif not states:
            filters["state"] = ("new", "regressed")
        rows = store.findings(filter=filters)
        return 200, {"findings": _jsonable(rows[:MAX_ROWS])}
    if route_path == "/api/v1/projects":
        return 200, {"projects": _jsonable(store.all_projects())}
    if route_path.startswith("/api/v1/projects/"):
        project_id = route_path.removeprefix("/api/v1/projects/").strip("/")
        detail = store.project_detail(project_id)
        return (200, {"project": _jsonable(detail)}) if detail else (
            404, {"error": {"code": "not_found", "message": "Project not found"}}
        )
    if route_path == "/api/v1/prompts":
        project = params.get("project", [None])[0]
        prompt_type = params.get("type", [None])[0]
        return 200, {"prompts": _jsonable(store.prompt_artifacts(project, prompt_type))}
    if route_path == "/api/v1/schedule":
        return 200, {"schedule": _jsonable(store.schedule_state())}
    if route_path == "/api/v1/monitor":
        events = store.topic_events(0, limit=_int_param(params, "limit"))
        return 200, {"events": _jsonable(events)}
    return 404, {"error": {"code": "not_found", "message": "Route not found"}}


def dispatch(store: ReadStore, method: str, path: str, query: str = "") -> tuple[int, dict]:
    if method not in {"GET", "HEAD"}:
        return 405, {"error": {"code": "method_not_allowed", "message": "Method not allowed"}}
    return route(store, path, query)


# Keep import available for callers that need a stable serialization helper.
def dumps(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
