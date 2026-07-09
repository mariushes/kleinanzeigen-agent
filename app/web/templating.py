"""Shared Jinja2 templates instance, imported by every route module.

Kept out of `main.py` so the route modules (`app/web/routes/`) don't have to import the
app object to render — they only need the templates. `main.py` stays app-assembly only.
"""

from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR

templates = Jinja2Templates(directory=BASE_DIR / "app" / "web" / "templates")
