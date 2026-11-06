from flasgger import Swagger
from flask import Flask

_HEALTH_RESPONSE = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["healthy", "degraded"],
            "description": "Degraded when required tools are unavailable.",
        },
        "timestamp": {"type": "string", "format": "date-time"},
    },
}

_SWAGGER_TEMPLATE = {
    "swagger": "2.0",
    "info": {
        "title": "backend-template API",
        "description": "API documentation for the backend-template project.",
        "version": "0.1.0",
    },
    "basePath": "/",
    "schemes": ["http", "https"],
    "consumes": ["application/json"],
    "produces": ["application/json"],
    "securityDefinitions": {
        "ApiKeyAuth": {
            "type": "apiKey",
            "in": "header",
            "name": "Authorization",
            "description": "API key for authentication. Set SECRET_KEY in .env (default: '').",
        }
    },
    "security": [{"ApiKeyAuth": []}],
    "definitions": {
        "HealthResponse": _HEALTH_RESPONSE,
    },
}


def init_swagger(app: Flask) -> Swagger:
    return Swagger(
        app,
        template=_SWAGGER_TEMPLATE,
        config={
            "headers": [],
            "specs": [
                {
                    "endpoint": "apispec",
                    "route": "/backend-template/apispec.json",
                    "rule_filter": lambda _r: True,
                    "model_filter": lambda _t: True,
                }
            ],
            "static_url_path": "/flasgger_static",
            "swagger_ui": True,
            "specs_route": "/backend-template/docs/",
        },
    )
