import hashlib
import hmac
import os
import secrets
from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from .db import get_db
from .models import User

SESSION_SECRET = os.getenv("SESSION_SECRET", "unsafe-development-only-change-me")
SESSION_AGE = 12 * 60 * 60

def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + ":" + digest.hex()

def verify_password(password, value):
    try:
        salt, original = (bytes.fromhex(x) for x in value.split(":"))
        candidate = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
        return hmac.compare_digest(original, candidate)
    except (ValueError, TypeError):
        return False

def signer():
    return URLSafeTimedSerializer(SESSION_SECRET, salt="fieldpulse-session-v1")

def current_user(request: Request, db: Session = Depends(get_db)):
    cookie = request.cookies.get("fp_session")
    if not cookie:
        raise HTTPException(401, detail="Войдите в систему")
    try:
        user_id = signer().loads(cookie, max_age=SESSION_AGE)
    except (BadSignature, SignatureExpired):
        raise HTTPException(401, detail="Сессия недействительна")
    user = db.get(User, user_id)
    if not user or not user.active:
        raise HTTPException(401, detail="Пользователь недоступен")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        c = request.cookies.get("fp_csrf")
        if not c or not hmac.compare_digest(c, request.headers.get("X-CSRF-Token", "")):
            raise HTTPException(403, detail="Не пройдена проверка CSRF")
    return user

def roles(*names):
    def guard(user: User = Depends(current_user)):
        if user.role not in names:
            raise HTTPException(403, detail="Недостаточно прав")
        return user
    return guard

def access_ticket(db, user, ticket):
    if not ticket:
        raise HTTPException(404, detail="Заявка не найдена")
    if user.role == "client" and user.org_id != ticket.org_id:
        raise HTTPException(404, detail="Заявка не найдена")
    if user.role == "engineer" and ticket.assignee_id != user.id:
        raise HTTPException(404, detail="Заявка не найдена")
    return ticket

def access_site(db, user, site):
    if not site or (user.role == "client" and site.org_id != user.org_id):
        raise HTTPException(404, detail="Объект не найден")
    if user.role == "engineer":
        raise HTTPException(403, detail="Недостаточно прав")
    return site
