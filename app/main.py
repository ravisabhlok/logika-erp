import os

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.auth import NotAuthenticatedException, PermissionDeniedException, get_current_user
from app.routers import (
    auth_router, dashboard, customers, vendors, items, sales, invoices, purchase, bills, inventory,
    production, roles, users, audit,
)

app = FastAPI(title="Logika Systems ERP")

app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")

# Cache-busting token for static assets: the mtime of custom.css, appended
# as a ?v= query param on its <link> tag in base.html. Without this, a
# browser that already fetched custom.css keeps using its cached copy
# indefinitely (no server-side change ever alters the URL), so a sidebar/
# style fix deployed here can silently fail to appear for LAN users until
# they manually hard-refresh -- this happened with the Purchase dropdown
# grouping (2026-09-09). Changing the file changes its mtime, which changes
# the URL, which forces every browser to re-fetch it on the next load.
_custom_css_path = os.path.join(os.path.dirname(__file__), "static", "custom.css")
try:
    STATIC_VERSION = int(os.path.getmtime(_custom_css_path))
except OSError:
    STATIC_VERSION = 0
templates.env.globals["static_version"] = STATIC_VERSION


@app.exception_handler(NotAuthenticatedException)
async def not_authenticated_handler(request: Request, exc: NotAuthenticatedException):
    return RedirectResponse(url="/login", status_code=303)


@app.exception_handler(PermissionDeniedException)
async def permission_denied_handler(request: Request, exc: PermissionDeniedException):
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        user = get_current_user(request, db)
    finally:
        db.close()
    return templates.TemplateResponse(
        "403.html",
        {"request": request, "user": user, "module_label": exc.module_label, "action": exc.action},
        status_code=403,
    )


app.include_router(auth_router.router)
app.include_router(dashboard.router)
app.include_router(customers.router)
app.include_router(vendors.router)
app.include_router(items.router)
app.include_router(sales.router)
app.include_router(invoices.router)
app.include_router(purchase.router)
app.include_router(bills.router)
app.include_router(inventory.router)
app.include_router(production.router)
app.include_router(roles.router)
app.include_router(users.router)
app.include_router(audit.router)
