# -*- coding: utf-8 -*-
# cython:language_level=3

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware


def flash(request: Request, category: str = "info", message: str = ""):
    if "flash_messages" not in request.session:
        request.session["flash_messages"] = []
    flash_message = {"message": message, "category": category, "read": False}
    request.session["flash_messages"].append(flash_message)


def get_flashed_messages(request: Request, with_categories: bool = True):
    if "flash_messages" in request.session:
        messages = request.session.pop("flash_messages", [])
        if with_categories:
            return messages
        return [msg["message"] for msg in messages]
    return []


class FlashMessageMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not hasattr(request, "session"):
            raise RuntimeError("SessionMiddleware is required but not found.")
        request.state.flashed_messages = []
        request.state.flashed_messages.extend(get_flashed_messages(request))
        response = await call_next(request)
        return response
