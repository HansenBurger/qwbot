from __future__ import annotations

from qwbot.config import load_settings
from qwbot.web import create_app

app = create_app(load_settings())
