"""Jinja2 environment: autoescape on, the bot's own number formats as filters.

Money and digits go through ``services.ux_format``, the same functions the
bot's screens use, so a figure reads the same in both places.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiohttp import web
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from services.ux_format import fa_toman, to_persian_digits
from web.formats import register as register_formats
from web.keys import TEMPLATES

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def build_environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
        # A typo in a template must fail the page, not print an empty cell.
        undefined=StrictUndefined,
    )
    env.filters["toman"] = fa_toman
    env.filters["fa"] = to_persian_digits
    register_formats(env)
    return env


def render(request: web.Request, name: str, *, status: int = 200, **context: Any) -> web.Response:
    html = request.app[TEMPLATES].get_template(name).render(**context)
    return web.Response(text=html, status=status, content_type="text/html", charset="utf-8")
