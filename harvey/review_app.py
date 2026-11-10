"""Gershon add-on: a private review page for emails waiting for approval.

The Harvey dashboard only answers on 127.0.0.1, so a reviewer away from the
server can't reach its Outbox. This small app does one job: list every email
in ``pending_review`` and let the reviewer edit, approve or reject each one,
one at a time. It is meant to sit behind HTTPS (Caddy) on its own name.

Safety:
- Nothing here sends mail. Approving marks ONE email ``approved``; Harvey's
  Sender sends it later through the normal checks, only if a mailbox is
  connected. There is no approve-all.
- Access needs the private link from the Harvey alert emails. The key rides
  in the URL #fragment (never sent to the server, so never in proxy logs or
  Referer); the page trades it once for an HttpOnly session cookie derived
  from it. Rotating ``data/review-token`` locks every old link and session.
- Every mutation must be a same-origin JSON POST from an allowed origin, so
  another site can't drive it with the reviewer's cookie.
- Its key is separate from the dashboard token, which never leaves the box.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from harvey import dashboard as dash
from harvey import dashboard_auth as auth
from harvey.paths import PROJECT_ROOT

REVIEW_TOKEN_FILE = PROJECT_ROOT / "data" / "review-token"
PAGE_FILE = Path(__file__).resolve().parent / "web" / "review.html"
COOKIE = "harvey_review"
COOKIE_MAX_AGE = 30 * 24 * 3600
DEFAULT_PORT = 5556

token_store = auth.TokenStore(REVIEW_TOKEN_FILE)


def public_url() -> str:
    """Where the page is reached from outside (HARVEY_REVIEW_URL), no slash."""
    return (os.environ.get("HARVEY_REVIEW_URL") or "").strip().rstrip("/")


def review_link(base: str | None = None) -> str:
    """The private link for the alert emails."""
    base = (base if base is not None else public_url()) or f"http://127.0.0.1:{DEFAULT_PORT}"
    return f"{base.rstrip('/')}/review#k={token_store.get()}"


def _session_value(token: str) -> str:
    return hmac.new(token.encode(), b"harvey-review-session-v1", hashlib.sha256).hexdigest()


def _allowed_hosts() -> set[str]:
    hosts = set()
    base = public_url()
    if base:
        hosts.add((urlsplit(base).netloc or "").lower())
    return hosts


def _host_ok(request: Request) -> bool:
    raw = request.headers.get("host", "")
    if raw.lower() in _allowed_hosts():
        return True
    host = auth.parse_host(raw)
    return host is not None and auth.is_loopback(host)


def _is_public_request(request: Request) -> bool:
    return request.headers.get("host", "").lower() in _allowed_hosts()


def _origin_ok(request: Request) -> bool:
    """POSTs must come from this page: same origin, JSON, never cross-site."""
    origin = (request.headers.get("origin") or "").lower()
    host = request.headers.get("host", "").lower()
    if not host:
        return False
    # The public name is HTTPS only; plain http is for the loopback address.
    allowed = {f"https://{host}"} if _is_public_request(request) else {f"http://{host}"}
    if origin not in allowed:
        return False
    site = (request.headers.get("sec-fetch-site") or "same-origin").lower()
    if site not in ("same-origin", "none"):
        return False
    return (request.headers.get("content-type") or "").split(";")[0].strip().lower() \
        == "application/json"


def _signed_in(request: Request) -> bool:
    try:
        token = token_store.get()
    except auth.TokenError:
        return False
    return hmac.compare_digest(request.cookies.get(COOKIE, ""), _session_value(token))


def _deny(status: int, message: str) -> JSONResponse:
    return JSONResponse({"success": False, "message": message}, status_code=status)


app = FastAPI(title="Harvey review", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    if not _host_ok(request):
        return _deny(400, "Unknown host.")
    path = request.url.path
    if request.method not in ("GET", "HEAD", "POST"):
        return _deny(405, "Method not allowed.")
    if request.method == "POST" and not _origin_ok(request):
        return _deny(403, "Cross-origin requests are refused.")
    if path.startswith("/review/api/") and not _signed_in(request):
        return _deny(401, "Open the newest link from a Harvey email to sign in.")
    response = await call_next(request)
    for k, v in auth.SECURITY_HEADERS:
        response.headers[k.decode()] = v.decode()
    if _is_public_request(request):
        response.headers["strict-transport-security"] = "max-age=31536000"
    return response


@app.get("/")
async def root():
    return RedirectResponse("/review", status_code=303)


@app.get("/review", response_class=HTMLResponse)
async def page():
    """The page shell. It carries no data; the data needs the session."""
    return PAGE_FILE.read_text(encoding="utf-8")


@app.post("/review/session")
async def sign_in(request: Request):
    """Trade the key from the link's #fragment for a session cookie."""
    try:
        body = await request.json()
    except ValueError:
        body = None
    key = str(body.get("key") or "") if isinstance(body, dict) else ""
    try:
        token = token_store.get()
    except auth.TokenError as e:
        return _deny(503, str(e))
    if not key or not hmac.compare_digest(key, token):
        await asyncio.sleep(0.5)  # slows guessing; the key is 256 bits anyway
        return _deny(401, "This link is not valid any more. Open the newest Harvey email.")
    resp = JSONResponse({"success": True})
    resp.set_cookie(COOKIE, _session_value(token), max_age=COOKIE_MAX_AGE, httponly=True,
                    secure=_is_public_request(request), samesite="lax", path="/review")
    return resp


@app.post("/review/logout")
async def sign_out():
    resp = JSONResponse({"success": True})
    resp.delete_cookie(COOKIE, path="/review")
    return resp


@app.get("/review/api/pending")
async def pending():
    state = dash._state()
    await state.init_db()
    from harvey.state import HELD_FOR_REVIEW
    items = await dash._with_context(await state.get_outbox(status="pending_review", limit=500))
    out = []
    for i in items:
        err = i.get("error") or ""
        out.append({
            "id": i["id"], "to_email": i.get("to_email", ""), "subject": i.get("subject", ""),
            "body": i.get("body", ""), "kind": i.get("kind", ""), "step": i.get("step"),
            "created_at": i.get("created_at", ""), "context": i.get("context", {}),
            "held": err[len(HELD_FOR_REVIEW):] if err.startswith(HELD_FOR_REVIEW) else "",
        })
    view = dash._compliance_view()
    return {"items": out, "footer": view["footer"], "can_send": dash._harvey_can_send(),
            "paused": bool(await state.get_setting("sending_paused"))}


async def _pending_item(item_id: str):
    state = dash._state()
    await state.init_db()
    item = await state.get_outbox_item(item_id)
    if not item or item.get("status") != "pending_review":
        return state, None
    return state, item


@app.post("/review/api/{item_id}/edit")
async def edit(item_id: str, request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    return await dash.edit_pending_draft(item_id, body, actor="review_page")


@app.post("/review/api/{item_id}/approve")
async def approve(item_id: str):
    """Approve this ONE email. It is sent later by Harvey's Sender, never here."""
    state, item = await _pending_item(item_id)
    if not item:
        return _deny(409, "This email is no longer waiting for approval.")
    if not await state.approve_outbox(item_id):
        return _deny(409, "This email is no longer waiting for approval.")
    await state.log_action("outbox_approved", "review_page",
                           {"outbox_id": item_id, "to_email": item.get("to_email", "")})
    return {"success": True}


@app.post("/review/api/{item_id}/reject")
async def reject(item_id: str):
    state, item = await _pending_item(item_id)
    if not item or not await state.reject_outbox_item(item_id):
        return _deny(409, "This email is no longer waiting for approval.")
    await state.log_action("outbox_rejected", "review_page",
                           {"outbox_id": item_id, "to_email": item.get("to_email", "")})
    return {"success": True}


def start_review_page(host: str = "127.0.0.1", port: int = DEFAULT_PORT):
    """Serve on loopback only; HTTPS and the public name come from Caddy."""
    import uvicorn
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    bind = dash._check_loopback_bind(host)
    token_store.get()  # fail now, not on the first visit
    print(f"\n  Harvey review page on http://{host}:{port}/review")
    print(f"  Private link (keep it private): {review_link()}\n")
    uvicorn.run(app, host=bind, port=port, log_level="warning",
                proxy_headers=False, server_header=False, date_header=False)
