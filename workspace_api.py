from __future__ import annotations

from modules import workspace_routes
from workspace_store import WorkspaceError, changes, entities, entity, scrub
from workspace_views import (chat_analysis, home, integration_preview,
                             integration_status, notification_preview, people,
                             projects)
from workspace_write import entity_edit, entity_review, person, project_review, project_topics
from workspace_search import search

_MODULE_ROUTES = workspace_routes()


def get(db, path: str, params: dict):
    match path:
        case "home":
            return home(db, params)
        case "analysis":
            return chat_analysis(db, params)
        case "projects":
            return projects(db)
        case "project":
            item = next((p for p in projects(db)["items"] if p["name"] == params.get("project")), None)
            if item is None:
                raise WorkspaceError("Проект не найден", 404)
            return item
        case "entities":
            return entities(db, params)
        case "entity":
            return entity(db, int(params.get("id", 0)))
        case "changes":
            return changes(db, params)
        case "people":
            return people(db)
        case "search":
            return search(db, params)
        case "integration/101/status":
            return integration_status(db)
        case "integration/101/preview":
            return integration_preview(db, params)
        case "notifications/preview":
            return notification_preview(db, params)
        case _:
            fn = _MODULE_ROUTES.get(("GET", path))
            if fn is None:
                raise WorkspaceError("Маршрут не найден", 404)
            return fn(db, params)


def post(db, path: str, body: dict):
    match path:
        case "entity/edit":
            return entity_edit(db, body)
        case "entity/review":
            return entity_review(db, body)
        case "project/review":
            return project_review(db, body)
        case "project/topics":
            return project_topics(db, body)
        case "people":
            return person(db, body)
        case _:
            fn = _MODULE_ROUTES.get(("POST", path))
            if fn is None:
                raise WorkspaceError("Маршрут записи не найден", 404)
            return fn(db, body)


def dispatch(db, method: str, path: str, params: dict) -> tuple[dict, int]:
    try:
        if not isinstance(params, dict):
            raise WorkspaceError("Ожидается JSON-объект")
        result = get(db, path, params) if method == "GET" else post(db, path, params)
        return scrub(result), 200
    except WorkspaceError as exc:
        return {"ok": False, "error": str(exc)}, exc.code
    except (ValueError, TypeError, KeyError):
        return {"ok": False, "error": "Некорректные параметры запроса"}, 400
