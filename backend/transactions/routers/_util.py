"""Shared router utilities."""

from __future__ import annotations

import json

from fastapi.responses import JSONResponse

from encoder.json_encoder import MyJSONEncoder


def to_json_response(data) -> JSONResponse:
    """Serialize data containing ObjectId/datetime to a JSONResponse."""
    return JSONResponse(content=json.loads(json.dumps(data, cls=MyJSONEncoder)))
