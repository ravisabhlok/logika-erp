from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.auth import verify_password, hash_password, get_current_user, require_login

router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/login")
def login_form(request: Request, db: Session = Depends(get_db)):
    if get_current_user(request, db):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "user": None})


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username, User.is_active == True).first()  # noqa: E712
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "user": None, "error": "Invalid username or password"},
            status_code=401,
        )
    request.session["user_id"] = user.id
    return RedirectResponse(url="/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@router.get("/account")
def account_page(request: Request, user: User = Depends(require_login)):
    return templates.TemplateResponse("account.html", {"request": request, "user": user})


@router.post("/account/password")
def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_login),
):
    if not verify_password(current_password, user.password_hash):
        return RedirectResponse(url="/account?error=Current password is incorrect", status_code=303)
    if new_password != confirm_password:
        return RedirectResponse(url="/account?error=New passwords do not match", status_code=303)
    if len(new_password) < 6:
        return RedirectResponse(url="/account?error=Password must be at least 6 characters", status_code=303)

    user.password_hash = hash_password(new_password)
    db.commit()
    return RedirectResponse(url="/account?success=Password updated", status_code=303)
