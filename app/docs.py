from flasgger import Swagger
from flask import Flask

from app.models import ErrorResponse, GraphResponse, TreeResponse

_HEALTH_RESPONSE = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["healthy", "degraded", "unhealthy"],
            "description": "Degraded when the cache is down; unhealthy when the data source does not answer.",
        },
        "timestamp": {"type": "string", "format": "date-time"},
        "dependencies": {
            "type": "object",
            "properties": {
                "solr": {"type": "string", "enum": ["ok", "unavailable"]},
                "cache": {"type": "string", "enum": ["ok", "unavailable"]},
            },
        },
    },
}


def _definitions() -> dict:
    """Swagger 2.0 definitions generated from the response models, so the docs cannot drift from the code."""
    template = "#/definitions/{model}"
    definitions: dict = {}
    for model in (GraphResponse, TreeResponse, ErrorResponse):
        schema = model.model_json_schema(by_alias=True, ref_template=template, mode="serialization")
        definitions.update(schema.pop("$defs", {}))
        definitions[model.__name__] = schema
    return definitions


_SWAGGER_TEMPLATE = {
    "swagger": "2.0",
    "info": {
        "title": "iati-cloud-dcma API",
        "description": "API documentation for the iati-cloud-dcma project.",
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
            "description": "API key for authentication. Set SECRET_KEY in .env (required).",
        }
    },
    "security": [{"ApiKeyAuth": []}],
    "definitions": {"HealthResponse": _HEALTH_RESPONSE, **_definitions()},
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
                    "route": "/iati-cloud-dcma/apispec.json",
                    "rule_filter": lambda _r: True,
                    "model_filter": lambda _t: True,
                }
            ],
            "static_url_path": "/flasgger_static",
            "swagger_ui": True,
            "specs_route": "/iati-cloud-dcma/docs/",
        },
    )
