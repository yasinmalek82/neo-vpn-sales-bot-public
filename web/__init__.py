"""The Mini App panel: server-rendered pages on the bot's own aiohttp app.

Layer rule: ``web → services/ → database/``. A route reads or writes through a
service function and renders a template; it holds no price, limit or
validation rule, and imports nothing from ``bot/handlers``. The area rule is
`.claude/rules/web-panel.md`.
"""
