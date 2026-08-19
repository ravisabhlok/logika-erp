from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.auth import NotAuthenticatedException, PermissionDeniedException, get_current_user
from app.routers import (
    auth_router, dashboard, customers, vendors, items, sales, invoices, purchase, inventory,
    production, roles, users, audit,
)

app = FastAPI(title="Logika Systems ERP")

app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")


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
app.include_router(inventory.router)
app.include_router(production.router)
app.include_router(roles.router)
app.include_router(users.router)
app.include_router(audit.router)
