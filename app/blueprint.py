import logging
from datetime import datetime

from flask import Blueprint, jsonify

logger = logging.getLogger("app")

bp = Blueprint("iati-cloud-dcma", __name__)

AUTH_EXEMPT_PATHS: set[str] = {"/docs/", "/apispec.json", "/health-check"}


@bp.route("/health-check", methods=["GET"])
def health_check():
    """
    Service health check.
    ---
    tags:
        - Health
    summary: Returns the health status of the API and its dependencies.
    responses:
        200:
            description: Service health status.
            schema:
                $ref: '#/definitions/HealthResponse'
    """
    all_ok = True  # In real implementation, check the health of dependencies here

    return jsonify(
        {
            "status": ("healthy" if all_ok else "degraded"),  # NOSONAR NOQA: Remove when real checks are implemented
            "timestamp": datetime.now().isoformat(),
        }
    )


@bp.route("/iati-cloud-dcma-route", methods=["GET"])
def demo_route():
    """
    A demo route. Requires authentication.
    """
    return jsonify({"message": "This is a protected route in the iati-cloud-dcma."})
