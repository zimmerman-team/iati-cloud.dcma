import logging
from datetime import datetime

from flask import Blueprint, current_app, jsonify, request
from pydantic import ValidationError

from app.iati_cloud_dcma.budget import BudgetExhausted
from app.iati_cloud_dcma.service import EXTENSION_KEY, Services, chain_payload, health
from app.iati_cloud_dcma.source import SourceTimeout, SourceUnavailable
from app.iati_cloud_dcma.traversal import SeedNotFound
from app.models import ChainQuery

logger = logging.getLogger("app")
bp = Blueprint("iati-cloud-dcma", __name__)

AUTH_EXEMPT_PATHS: set[str] = {"/docs", "/docs/", "/apispec.json", "/health-check"}
CHAIN_PARAMETERS = ("activity", "mode", "format", "orgs", "max_depth", "max_nodes")


def _services() -> Services:
    """Central lookup for app's services, better typing and only one place to change the extension key."""
    return current_app.extensions[EXTENSION_KEY]


def _error(message: str, status: int):
    """A JSON error response, with a status code."""
    return jsonify({"error": message}), status


def _validation_message(exc: ValidationError) -> str:
    """What is wrong with the query, without echoing the input back."""
    problems = [f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()]
    return "Invalid query. " + "; ".join(problems)


@bp.route("/chain", methods=["GET"])
def chain():
    """
    The delivery chain around one IATI activity.
    ---
    tags:
        - Delivery chain
    summary: The activities that fund an activity and the activities it funds, followed recursively.
    description: >
        `full` returns the seed, everything above it, everything below everything above it, and the direct
        co-funders of those. `constrained` returns only the line of funding through the seed. A result that was cut
        off by a limit has `truncated: true` and says why in `truncation_reasons`. Needs the API key.
    parameters:
        - {name: activity, in: query, type: string, required: true, minLength: 1, maxLength: 255,
           description: "The seed: an IATI activity identifier, in any letter case. Trimmed; no control
           characters. URL-encode it."}
        - {name: mode, in: query, type: string, enum: [full, constrained], default: full}
        - {name: format, in: query, type: string, enum: [graph, tree], default: graph}
        - {name: orgs, in: query, type: string, enum: [identified, refs, all], default: identified,
           description: "Which organisation nodes to return: with an identifier only; also with free text typed
           into the ref; also with a name but no ref."}
        - {name: max_depth, in: query, type: integer, minimum: 1,
           description: "Hops per walk. Capped at the configured maximum."}
        - {name: max_nodes, in: query, type: integer, minimum: 1,
           description: "Nodes returned. Capped at the configured maximum."}
    responses:
        200:
            description: >
                The chain, as a graph (`format=graph`, schema below) or as a tree (`format=tree`, see the
                `TreeResponse` model). Swagger 2.0 cannot state one of two schemas.
            schema:
                $ref: '#/definitions/GraphResponse'
        400:
            description: A parameter is missing or invalid.
            schema:
                $ref: '#/definitions/ErrorResponse'
        401:
            description: The API key is missing or wrong.
            schema:
                $ref: '#/definitions/ErrorResponse'
        404:
            description: The activity is not published and no published activity references it.
            schema:
                $ref: '#/definitions/ErrorResponse'
        502:
            description: The data source returned an error, or could not be reached after the retries.
            schema:
                $ref: '#/definitions/ErrorResponse'
        504:
            description: The data source timed out, or the time budget ran out before the seed could be read.
            schema:
                $ref: '#/definitions/ErrorResponse'
    """
    try:
        query = ChainQuery.model_validate(
            {name: request.args[name] for name in CHAIN_PARAMETERS if name in request.args}
        )
    except ValidationError as exc:
        return _error(_validation_message(exc), 400)
    try:
        return jsonify(chain_payload(_services(), query))
    except SeedNotFound:
        return _error("Activity not found: it is not published and no published activity references it.", 404)
    except SourceUnavailable:
        return _error("The data source is unavailable.", 502)
    except (SourceTimeout, BudgetExhausted):
        return _error("The data source timed out.", 504)


@bp.route("/health-check", methods=["GET"])
def health_check():
    """
    Service health check.
    ---
    tags:
        - Health
    summary: Returns the health status of the API and its dependencies.
    security: []
    responses:
        200:
            description: Healthy, or degraded (the cache is down; requests are served uncached).
            schema:
                $ref: '#/definitions/HealthResponse'
        503:
            description: Unhealthy (the data source does not answer).
            schema:
                $ref: '#/definitions/HealthResponse'
    """
    status, dependencies = health(_services())
    body = {"status": status, "timestamp": datetime.now().isoformat(), "dependencies": dependencies}
    return jsonify(body), (503 if status == "unhealthy" else 200)
