import hmac
import logging

from flask import Flask, jsonify, request
from flask_cors import CORS

from app.blueprint import bp
from app.cache import ResponseCache, create_redis_client
from app.config import settings
from app.docs import init_swagger
from app.iati_cloud_dcma.service import EXTENSION_KEY, Services
from app.iati_cloud_dcma.source import SolrLinkSource, create_solr_client
from app.iati_cloud_dcma.traversal import Limits

logger = logging.getLogger("app")


def _is_api_key(candidate: str | None) -> bool:
    """Whether `candidate` is the configured API key, compared in constant time."""
    if not candidate:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), settings.secret_key.encode("utf-8"))


def default_services() -> Services:
    """The real dependencies, from the settings. Neither client connects until it is first used."""
    solr = create_solr_client(settings.solr_url, settings.solr_timeout)
    return Services(
        new_source=lambda: SolrLinkSource(client=solr, batch_size=settings.id_batch_size),
        cache=ResponseCache(create_redis_client(settings.redis_url), ttl=settings.cache_ttl),
        limits=Limits(max_depth=settings.max_depth, max_nodes=settings.max_nodes),
        request_timeout=settings.request_timeout,
    )


def create_app(services: Services | None = None) -> Flask:
    """Factory function to create and configure the Flask app. Tests pass their own `services`."""
    app = Flask(__name__)
    app.extensions[EXTENSION_KEY] = services if services is not None else default_services()
    CORS(app, origins=settings.cors_allowed_origins)
    app.register_blueprint(bp, url_prefix="/iati-cloud-dcma")

    @app.before_request
    def require_api_key():
        """Enforce API key auth, with exemptions for Swagger and auth-exempt paths."""
        if request.path.startswith("/flasgger_static") or request.path in settings.auth_exempt_paths:
            return
        if request.method == "OPTIONS":  # CORS preflights carry no credentials
            return
        if not _is_api_key(request.headers.get("Authorization")):
            return jsonify({"error": "Unauthorized"}), 401

    init_swagger(app)
    return app


app = create_app()

if __name__ == "__main__":  # pragma: no cover
    # Local development only; production runs under Gunicorn.
    app.run(host="127.0.0.1", port=settings.port, debug=settings.flask_debug)
