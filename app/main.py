import logging

from flask import Flask, jsonify, request
from flask_cors import CORS

from app.blueprint import bp
from app.config import settings
from app.docs import init_swagger

# Initialize main app logger
logger = logging.getLogger("app")


def create_app() -> Flask:
    """Factory function to create and configure the Flask app."""
    app = Flask(__name__)
    CORS(app, origins=settings.cors_allowed_origins)
    app.register_blueprint(bp, url_prefix="/iati-cloud-dcma")

    @app.before_request
    def require_api_key():
        """Enforce API key auth, with exemptions for Swagger and auth-exempt paths."""
        if request.path.startswith("/flasgger_static") or request.path in settings.auth_exempt_paths:
            return
        if request.headers.get("Authorization") != settings.secret_key:
            return jsonify({"error": "Unauthorized"}), 401

    # Initialize Swagger documentation
    init_swagger(app)
    return app


app = create_app()

if __name__ == "__main__":  # pragma: no cover
    # Initialize Flask app and register all blueprints
    app.run(host="127.0.0.1", port=settings.port, debug=settings.flask_debug)
