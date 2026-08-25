"""A small password-protected admin UI: contacts, callers, and the call log.

**This app is served on its own port and must never be proxied publicly.**
The public hostname has to be reachable by Twilio, so anything mounted on
that app is on the open internet. This one holds the contact book, phone numbers,
call history and settings, so it listens separately (`ADMIN_PORT`, published to 127.0.0.1 only)
and is reached over Tailscale. The password is the second lock, not the only one.

Auth is a password from the environment, exchanged for an HMAC-signed cookie.
There is no user database and no registration - one shared secret, one person.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import time
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .contacts import Contact, normalise
from .history import CallHistory, _humanise_age
from .notify import build_bridge
from .settings import BEHAVIOUR_KEYS, CHANNEL_NAMES, CHOICES, ROUTING_CATEGORIES
from .whatsapp import FLAVOURS

log = logging.getLogger(__name__)

COOKIE_NAME = "recadmin"
SESSION_TTL_S = 7 * 24 * 3600

# Brute-force damping. One password and no lockout is an invitation, even on a
# port that is not meant to be public - the whole point of defence in depth is
# that the other layer might be wrong.
MAX_ATTEMPTS = 8
LOCKOUT_S = 300

CATEGORY_LABELS = {
    "spam_telesales": "spam",
    "tradesperson_admin": "tradesperson",
    "delivery_appointment": "delivery",
    "recruiter_job_business": "recruiter",
    "family_friend_personal": "personal",
    "urgent": "urgent",
}


class _Attempts:
    def __init__(self) -> None:
        self._by_ip: dict[str, tuple[int, float]] = {}

    def locked(self, ip: str) -> int:
        count, first = self._by_ip.get(ip, (0, 0.0))
        if count < MAX_ATTEMPTS:
            return 0
        remaining = int(LOCKOUT_S - (time.time() - first))
        if remaining <= 0:
            self._by_ip.pop(ip, None)
            return 0
        return remaining

    def fail(self, ip: str) -> None:
        count, first = self._by_ip.get(ip, (0, 0.0))
        if count == 0 or time.time() - first > LOCKOUT_S:
            self._by_ip[ip] = (1, time.time())
        else:
            self._by_ip[ip] = (count + 1, first)

    def clear(self, ip: str) -> None:
        self._by_ip.pop(ip, None)


def _sign(secret: str, value: str) -> str:
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def _mint_cookie(secret: str) -> str:
    expiry = str(int(time.time() + SESSION_TTL_S))
    return f"{expiry}.{_sign(secret, expiry)}"


def _valid_cookie(secret: str, raw: str) -> bool:
    if not raw or "." not in raw:
        return False
    expiry, sig = raw.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(secret, expiry)):
        return False
    try:
        return time.time() < float(expiry)
    except ValueError:
        return False


CSS = """
:root { color-scheme: light dark;
  --bg:#f7f7f8; --card:#fff; --ink:#1a1c1f; --muted:#71757c; --faint:#9ba0a7;
  --line:#e7e8ec; --accent:#2f6fd0; --warn:#b8442a; --ok:#2f7a55;
  --shadow:0 1px 2px rgba(0,0,0,.05); }
@media (prefers-color-scheme: dark) { :root {
  --bg:#141518; --card:#1d1f23; --ink:#eceef0; --muted:#9aa0a8; --faint:#767c84;
  --line:#2b2e34; --accent:#7cb0f5; --warn:#e08a6b; --ok:#6fbf95; --shadow:none; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased; }

header { position:sticky; top:0; z-index:5; background:var(--card);
  border-bottom:1px solid var(--line); padding:0 16px; display:flex; gap:4px;
  align-items:center; }
header .brand { font-weight:600; margin-right:12px; padding:14px 0; }
header nav a { color:var(--muted); text-decoration:none; padding:15px 10px;
  display:inline-block; border-bottom:2px solid transparent; font-size:14px; }
header nav a:hover { color:var(--ink); }
header nav a.on { color:var(--ink); border-bottom-color:var(--accent); }
header .sp { flex:1; }
header form button { padding:6px 10px; font-size:13px; }

main { max-width:680px; margin:0 auto; padding:20px 16px 60px; }
h2 { font-size:13px; font-weight:600; margin:26px 0 10px; color:var(--muted);
  text-transform:uppercase; letter-spacing:.05em; }
h2:first-child { margin-top:0; }

.card { background:var(--card); border:1px solid var(--line); border-radius:12px;
  box-shadow:var(--shadow); margin-bottom:10px; overflow:hidden; }
.card > .pad { padding:14px 16px; }
a.card { display:block; color:inherit; text-decoration:none; }
a.card:hover { border-color:var(--accent); }

.top { display:flex; align-items:baseline; gap:8px; }
.top .name { font-weight:600; font-size:15.5px; }
.top .when { margin-left:auto; color:var(--faint); font-size:12.5px; white-space:nowrap; }
.sub { color:var(--muted); font-size:13px; margin-top:2px; }
.body { margin-top:8px; }
.muted { color:var(--muted); font-size:13px; }
.faint { color:var(--faint); font-size:12.5px; }

.tag { display:inline-block; font-size:11.5px; padding:2px 8px; border-radius:6px;
  background:var(--bg); color:var(--muted); margin:8px 6px 0 0; }
.tag.urgent { background:color-mix(in srgb, var(--warn) 14%, transparent); color:var(--warn); }
.tag.ok { background:color-mix(in srgb, var(--ok) 14%, transparent); color:var(--ok); }

.field { display:block; margin-bottom:10px; }
.field span { display:block; font-size:12.5px; color:var(--muted); margin-bottom:4px; }
input, textarea, select { font:inherit; width:100%; padding:9px 11px; border:1px solid var(--line);
  border-radius:9px; background:var(--bg); color:var(--ink); }
input:focus, textarea:focus, select:focus { outline:2px solid color-mix(in srgb,var(--accent) 40%,transparent);
  outline-offset:-1px; border-color:var(--accent); }
input[type=checkbox] { width:auto; margin:0; }

/* Channel and settings screens */
.chk { display:flex; align-items:center; gap:8px; font-size:14px; margin-bottom:10px; }
.chk input { flex:none; }
.head { display:flex; align-items:center; gap:8px; padding:13px 16px;
  border-bottom:1px solid var(--line); }
.head .title { font-weight:600; }
.head .sp { flex:1; }
.pill { font-size:11.5px; padding:2px 9px; border-radius:20px; background:var(--bg);
  color:var(--muted); white-space:nowrap; }
.pill.on { background:color-mix(in srgb,var(--ok) 15%,transparent); color:var(--ok); }
.pill.off { background:color-mix(in srgb,var(--warn) 13%,transparent); color:var(--warn); }
.matrix { width:100%; border-collapse:collapse; font-size:13px; }
.matrix th, .matrix td { padding:7px 6px; border-bottom:1px solid var(--line); text-align:center; }
.matrix th:first-child, .matrix td:first-child { text-align:left; padding-left:0; }
.matrix thead th { font-size:11.5px; color:var(--muted); font-weight:600; text-transform:uppercase;
  letter-spacing:.04em; }
.matrix tbody tr:last-child td { border-bottom:0; }
.matrix tr.dflt td { font-weight:600; background:var(--bg); }
.ok-msg { color:var(--ok); font-size:13px; margin-top:8px; }
.qr { text-align:center; padding:8px 0 4px; }
.qr img { width:264px; max-width:100%; image-rendering:pixelated; border-radius:9px;
  background:#fff; padding:10px; }
.mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12.5px; }
textarea { min-height:64px; resize:vertical; }
.two { display:flex; gap:10px; } .two > * { flex:1; min-width:0; }
button { font:inherit; font-weight:500; padding:9px 15px; border:0; border-radius:9px;
  background:var(--accent); color:#fff; cursor:pointer; }
button:hover { filter:brightness(1.08); }
button.ghost { background:transparent; color:var(--muted); border:1px solid var(--line); }
button.ghost:hover { color:var(--ink); border-color:var(--muted); filter:none; }
.actions { display:flex; gap:8px; align-items:center; margin-top:4px; }
form.inline { display:inline; }

.bar { display:flex; align-items:center; gap:8px; margin-bottom:14px; }
.bar .sp { flex:1; }
.chip { display:inline-flex; gap:6px; align-items:center; padding:6px 12px; border-radius:20px;
  border:1px solid var(--line); background:var(--card); color:var(--muted);
  text-decoration:none; font-size:13px; }
.chip span { color:var(--faint); font-variant-numeric:tabular-nums; }
.chip.on { border-color:var(--accent); color:var(--ink); }
.btn { display:inline-block; padding:8px 14px; border-radius:9px; background:var(--accent);
  color:#fff; text-decoration:none; font-size:13.5px; font-weight:500; }
.btn.ghost { background:transparent; color:var(--muted); border:1px solid var(--line); }
.dir { display:inline-block; font-size:11.5px; font-weight:500; padding:2px 9px;
  border-radius:6px; }
.dir.in { background:var(--bg); color:var(--muted); }
.dir.out { background:color-mix(in srgb, var(--accent) 15%, transparent); color:var(--accent); }
.note { border-top:1px solid var(--line); padding:11px 16px; }

/* Setup wizard */
.wizsteps { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:14px; }
.wizsteps a { display:inline-flex; align-items:center; gap:7px; padding:7px 13px;
  border-radius:20px; border:1px solid var(--line); background:var(--card);
  color:var(--muted); text-decoration:none; font-size:13px; }
.wizsteps a b { display:inline-flex; align-items:center; justify-content:center;
  width:18px; height:18px; border-radius:50%; background:var(--bg);
  font-size:11px; font-weight:600; }
.wizsteps a.on { border-color:var(--accent); color:var(--ink); }
.wizsteps a.on b { background:var(--accent); color:#fff; }
.wiznote { border:1px solid var(--line); border-left:3px solid var(--accent);
  background:var(--card); border-radius:9px; padding:11px 14px; margin-bottom:14px;
  font-size:13px; color:var(--muted); }
.field span small { color:var(--faint); font-weight:400; }
.empty { color:var(--muted); text-align:center; padding:28px 16px; }
a.num { color:var(--accent); text-decoration:none; font-variant-numeric:tabular-nums; }
.hint { color:var(--faint); font-size:12.5px; margin:-4px 0 14px; }
.err { color:var(--warn); font-size:13px; margin-top:8px; }
@media (max-width:520px) { .two { display:block; } .two > * { margin-bottom:10px; } }
"""


def _page(title: str, body: str, nav: bool = True, here: str = "") -> HTMLResponse:
    def tab(href: str, label: str, key: str) -> str:
        on = " class=on" if key == here else ""
        return f'<a href="{href}"{on}>{label}</a>'

    header = (
        '<header><span class="brand">Receptionist</span><nav>'
        + tab("/", "Callers", "callers")
        + tab("/calls", "Calls", "calls")
        + tab("/settings", "Settings", "settings")
        + '</nav><span class="sp"></span>'
        '<form class="inline" method="post" action="/logout">'
        '<button class="ghost">Sign out</button></form></header>'
        if nav
        else '<header><span class="brand">Receptionist</span></header>'
    )
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset=utf-8>"
        f'<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
        f"<body>{header}<main>{body}</main></body></html>"
    )


def _esc(value) -> str:
    return html.escape(str(value or ""))


def _clip(text, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_admin_app(
    password: str,
    secret: str,
    contacts_path: Path,
    history: CallHistory,
    country_code: str = "44",
    cfg=None,
    notifier=None,
    secrets=None,
) -> FastAPI:
    admin = FastAPI(title="ai-receptionist-admin", docs_url=None, redoc_url=None,
                    openapi_url=None)
    attempts = _Attempts()

    def _authed(request: Request) -> bool:
        return _valid_cookie(secret, request.cookies.get(COOKIE_NAME, ""))

    def _load() -> dict[str, Contact]:
        """Saved contacts, keyed by normalised number."""
        try:
            raw = json.loads(contacts_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.error("admin could not read contacts: %s", exc)
            return {}

        out: dict[str, Contact] = {}
        for number, value in (raw or {}).items():
            key = normalise(str(number), country_code)
            if not key:
                continue
            if isinstance(value, dict):
                out[key] = Contact(
                    number=str(number),
                    name=str(value.get("name") or ""),
                    nickname=str(value.get("nickname") or ""),
                    relationship=str(value.get("relationship") or ""),
                    notes=str(value.get("notes") or ""),
                )
            else:
                out[key] = Contact(number=str(number), name=str(value or ""))
        return out

    def _save(entries: dict[str, Contact]) -> bool:
        payload = {
            (c.number or f"+{key}"): c.to_json()
            for key, c in sorted(entries.items(), key=lambda kv: kv[1].label.lower())
        }
        try:
            contacts_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            return True
        except OSError as exc:
            # The likely cause is the config volume still being mounted read-only.
            log.error("admin could not write contacts: %s", exc)
            return False

    # -- auth --------------------------------------------------------------

    @admin.get("/login")
    async def login_form(request: Request, bad: int = 0) -> Response:
        if _authed(request):
            return RedirectResponse("/", status_code=303)
        wait = attempts.locked(request.client.host if request.client else "?")
        msg = ""
        if wait:
            msg = f'<div class="err">Too many attempts. Try again in {wait}s.</div>'
        elif bad:
            msg = '<div class="err">Wrong password.</div>'
        return _page(
            "Sign in",
            '<div class="card"><div class="pad"><form method="post" action="/login">'
            '<label class="field"><span>Password</span>'
            '<input type="password" name="password" autofocus '
            'autocomplete="current-password"></label>'
            f'<div class="actions"><button>Sign in</button></div>{msg}'
            "</form></div></div>",
            nav=False,
        )

    @admin.post("/login")
    async def login(
        request: Request, password_field: str = Form("", alias="password")
    ) -> Response:
        ip = request.client.host if request.client else "?"
        if attempts.locked(ip):
            return RedirectResponse("/login", status_code=303)
        if not hmac.compare_digest(password_field, password):
            attempts.fail(ip)
            log.warning("admin login failed from %s", ip)
            return RedirectResponse("/login?bad=1", status_code=303)

        attempts.clear(ip)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE_NAME, _mint_cookie(secret), max_age=SESSION_TTL_S,
            httponly=True, samesite="lax",
        )
        log.info("admin login ok from %s", ip)
        return response

    @admin.post("/logout")
    async def logout() -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME)
        return response

    # -- shared rendering --------------------------------------------------

    def _tags(rec: dict) -> str:
        """At most a couple per call. Everything tagged is nothing tagged."""
        out = []
        if rec.get("urgency") == "high" or rec.get("flagged_urgent"):
            out.append('<span class="tag urgent">urgent</span>')
        category = rec.get("category", "")
        if category and category != "unknown":
            out.append(f'<span class="tag">{_esc(CATEGORY_LABELS.get(category, category))}</span>')
        if rec.get("transfer_connected"):
            out.append('<span class="tag ok">put through</span>')
        elif rec.get("transfer_attempted"):
            out.append('<span class="tag">missed transfer</span>')
        return "".join(out)

    def _direction(rec: dict) -> str:
        """Which way the call went, said plainly.

        Without this a callback reads exactly like an incoming call, which is
        confusing precisely when it matters: after a reply to a summary and
        the agent rings someone back on his behalf.
        """
        if rec.get("direction") == "outbound":
            return ('<span class="dir out">&#8599; We rang them</span>')
        return '<span class="dir in">&#8600; They rang in</span>'

    # -- callers -----------------------------------------------------------

    @admin.get("/")
    async def callers_page(request: Request, only: str = "") -> Response:
        """Everyone, in one list.

        Saved contacts and people who have merely rung are the same kind of thing
        - a person with a number - so they share a list and a page rather than
        living in two parallel sections that have to be kept in step.
        """
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)

        saved = _load()
        rows = {row["key"]: row for row in history.callers()}
        # Saved contacts who have never rung still belong in the list; without
        # this they would have no way in at all.
        for key, contact in saved.items():
            rows.setdefault(key, {
                "key": key, "number": contact.number, "calls": 0,
                "last_at": "", "last_age": "", "last_summary": "",
            })

        contacts_only = only == "contacts"
        ordered = sorted(
            rows.values(),
            # Recent callers first; never-called contacts after them, by name.
            key=lambda r: (r["last_at"] or "", r["key"]),
            reverse=True,
        )

        blocks = []
        for row in ordered:
            contact = saved.get(row["key"])
            if contacts_only and not contact:
                continue
            suggested = history.suggested_name(row["key"])
            who = (contact.label if contact else "") or suggested or "Unknown caller"

            sub = []
            if contact and contact.relationship:
                sub.append(_esc(contact.relationship))
            sub.append(
                f'{row["calls"]} call{"" if row["calls"] == 1 else "s"}'
                if row["calls"] else "no calls yet"
            )
            gist = _clip(row.get("last_summary"), 110)
            flag = ('<span class="tag ok">contact</span>' if contact
                    else '<span class="tag">not saved</span>')

            blocks.append(
                f'<a class="card" href="/caller/{_esc(row["key"])}"><div class="pad">'
                f'<div class="top"><span class="name">{_esc(who)}</span>'
                f'<span class="when">{_esc(row["last_age"])}</span></div>'
                f'<div class="sub">{" · ".join(sub)}</div>'
                + (f'<div class="body muted">{_esc(gist)}</div>' if gist else "")
                + f"<div>{flag}</div></div></a>"
            )

        saved_count = len(saved)
        controls = (
            '<div class="bar">'
            f'<a class="chip{"" if contacts_only else " on"}" href="/">'
            f"Everyone <span>{len(rows)}</span></a>"
            f'<a class="chip{" on" if contacts_only else ""}" href="/?only=contacts">'
            f"Contacts <span>{saved_count}</span></a>"
            '<span class="sp"></span>'
            '<a class="btn" href="/contact/new">Add contact</a>'
            "</div>"
        )
        empty = ('<div class="card"><div class="empty">'
                 + ("No contacts saved yet." if contacts_only else "No calls yet.")
                 + "</div></div>")
        return _page("Callers", controls + ("".join(blocks) or empty), here="callers")

    @admin.get("/contact/new")
    async def contact_new(request: Request, err: int = 0) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        err_msg = ('<div class="err">Could not save — check the number, and that '
                   "the config volume is writable.</div>") if err else ""
        return _page(
            "Add contact",
            '<div class="card"><div class="pad"><form method="post" action="/contacts/save">'
            '<div class="two">'
            '<label class="field"><span>Full name</span><input name="name" autofocus></label>'
            '<label class="field"><span>What you call them</span>'
            '<input name="nickname" placeholder="optional"></label></div>'
            '<div class="two">'
            '<label class="field"><span>Number</span>'
            '<input name="number" type="tel" placeholder="07700 900123"></label>'
            '<label class="field"><span>Relationship</span>'
            '<input name="relationship" placeholder="mum · plumber · colleague"></label>'
            "</div>"
            '<label class="field"><span>Context for the agent</span>'
            '<textarea name="notes" placeholder="Anything worth knowing when they ring."'
            "></textarea></label>"
            f'<div class="actions"><button>Save contact</button>'
            '<a class="btn ghost" href="/">Cancel</a></div>'
            f"{err_msg}</form></div></div>",
        )

    @admin.get("/caller/{key}")
    async def caller_profile(request: Request, key: str, err: int = 0) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)

        saved = _load()
        contact = saved.get(key)
        profile = history.profile(key)
        # A saved contact who has never rung still gets a page: this is where
        # contact details and notes are edited, so making it depend on call
        # history would leave exactly the people most worth annotating -
        # family, the plumber he has just saved - with no way in.
        if not profile and not contact:
            return _page("Unknown caller", '<div class="card"><div class="empty">'
                         "Nothing here — no calls from that number, and it is not "
                         "saved as a contact.</div></div>")

        who = ((contact.label if contact else "")
               or (profile["suggested_name"] if profile else "")
               or "Unknown caller")
        number = contact.number if contact else profile["number"]

        # Header: who they are, and the one-line shape of their history.
        if profile:
            facts = [f'{profile["total"]} call{"" if profile["total"] == 1 else "s"}']
            facts.append(
                f'since {_esc(profile["first_age"])}' if profile["total"] > 1
                else _esc(profile["last_age"])
            )
        else:
            facts = ["no calls yet"]
        if contact and contact.relationship:
            facts.insert(0, _esc(contact.relationship))

        # Two saved contacts a digit apart is almost always one person saved
        # twice - which also stops the history fold working, because both
        # numbers now look authoritative.
        from .contacts import near_miss
        already_merged = set(history.merged_into(key))
        twins = [
            c for k, c in saved.items()
            # Nothing to warn about once they have been merged - that is the fix
            # this hint exists to prompt.
            if k != key and k not in already_merged and near_miss(k, key)
        ]
        warn = ""
        if twins:
            other = twins[0]
            warn = (
                '<div class="card"><div class="pad"><div class="muted">'
                f'<b>{_esc(other.label)}</b> is saved on {_esc(other.number)} — one '
                "digit from this one, so this is probably the same person saved "
                "twice. Removing whichever is wrong lets their calls group together "
                "again.</div></div></div>"
            )

        # A merged profile has more than one number, and which one they rang on
        # matters - "the mobile" and "the landline" are different situations.
        all_numbers = history.numbers_for(key) if profile else [number]
        if number not in all_numbers:
            all_numbers.insert(0, number)
        number_links = " · ".join(
            f'<a class="num" href="tel:{_esc(n)}">{_esc(n)}</a>' for n in all_numbers
        )

        head = (
            '<div class="card"><div class="pad">'
            f'<div class="top"><span class="name" style="font-size:18px">{_esc(who)}</span></div>'
            f'<div class="sub">{number_links}'
            f' · {" · ".join(facts)}</div>'
            + (f'<div class="body muted">{_esc(contact.notes)}</div>'
               if contact and contact.notes else "")
            + "</div></div>"
        )

        # Contact details, editable in place. Pre-filled with our best guess when
        # they are not saved yet, which is the whole point of suggesting a name.
        err_msg = ('<div class="err">Could not save — the config volume is '
                   "mounted read-only.</div>") if err else ""
        form = (
            '<div class="card"><div class="pad">'
            f'<form method="post" action="/contacts/save">'
            f'<input type="hidden" name="number" value="{_esc(number)}">'
            f'<input type="hidden" name="back" value="{_esc(key)}">'
            '<div class="two">'
            '<label class="field"><span>Full name</span>'
            f'<input name="name" value="{_esc(contact.name if contact else profile["suggested_name"])}"'
            ' placeholder="Sarah Bennett"></label>'
            '<label class="field"><span>What you call them</span>'
            f'<input name="nickname" value="{_esc(contact.nickname if contact else "")}"'
            ' placeholder="Sarah"></label></div>'
            '<label class="field"><span>Relationship</span>'
            f'<input name="relationship" value="{_esc(contact.relationship if contact else "")}"'
            ' placeholder="partner · mum · plumber · colleague at Hays"></label>'
            '<label class="field"><span>Context for the agent</span>'
            f'<textarea name="notes" placeholder="Anything worth knowing when they '
            f'ring — what they usually want, how to treat them.">'
            f'{_esc(contact.notes if contact else "")}</textarea></label>'
            '<div class="actions"><button>'
            + ("Save changes" if contact else "Save contact")
            + "</button>"
            + (
                '<form class="inline" method="post" action="/contacts/delete">'
                f'<input type="hidden" name="number" value="{_esc(number)}">'
                '<button class="ghost" formaction="/contacts/delete">Remove</button>'
                "</form>"
                if contact else ""
            )
            + f"</div>{err_msg}</form></div></div>"
        )

        notes = "".join(
            f'<div class="note"><div class="top"><span>{_esc(n.get("text"))}</span>'
            '<span class="when">'
            '<form class="inline" method="post" action="/notes/delete">'
            f'<input type="hidden" name="key" value="{_esc(key)}">'
            f'<input type="hidden" name="at" value="{_esc(n.get("at"))}">'
            '<button class="ghost" style="padding:3px 8px;font-size:12px">Delete</button>'
            "</form></span></div>"
            f'<div class="faint">{_esc(_humanise_age(n.get("at", "")))}</div></div>'
            for n in (history.notes_for(number, 50))
        )
        notes_card = (
            '<div class="card"><div class="pad">'
            '<form method="post" action="/notes/add">'
            f'<input type="hidden" name="number" value="{_esc(number)}">'
            '<label class="field"><span>Add a note</span>'
            '<input name="text" placeholder="e.g. quoted £400, waiting on him"></label>'
            '<div class="actions"><button class="ghost">Add note</button></div>'
            "</form></div>" + notes + "</div>"
        )

        calls = []
        for rec in (profile["calls"] if profile else []):
            when = _humanise_age(rec.get("started_at", ""))
            stamp = str(rec.get("started_at", ""))[:16].replace("T", " ")
            gist = rec.get("summary") or rec.get("reason") or "No message taken."
            extra = ""
            if rec.get("requested_action"):
                extra = (f'<div class="faint" style="margin-top:6px">Asked for: '
                         f'{_esc(rec["requested_action"])}</div>')
            # Only worth showing when there is more than one, otherwise it is
            # the same number repeated down the page.
            came_from = ""
            if len(all_numbers) > 1 and rec.get("from_number"):
                preposition = "to" if rec.get("direction") == "outbound" else "on"
                came_from = (f'<div class="faint" style="margin-top:6px">{preposition} '
                             f'{_esc(rec["from_number"])}</div>')
            calls.append(
                '<div class="card"><div class="pad">'
                f'<div style="margin-bottom:6px">{_direction(rec)}</div>'
                f'<div class="top"><span class="name">{_esc(when)}</span>'
                f'<span class="when">{_esc(stamp)} · {int(rec.get("duration_s") or 0)}s</span></div>'
                f'<div class="body">{_esc(gist)}</div>{extra}{came_from}'
                f'<div class="top" style="margin-top:2px"><div>{_tags(rec)}</div>'
                '<span class="when">'
                '<form class="inline" method="post" action="/calls/delete" '
                "onsubmit=\"return confirm('Delete this call from the history?')\">"
                f'<input type="hidden" name="call_sid" value="{_esc(rec.get("call_sid"))}">'
                f'<input type="hidden" name="back" value="{_esc(key)}">'
                '<button class="ghost" style="padding:4px 9px;font-size:12px">Delete</button>'
                "</form></span></div></div></div>"
            )

        # Merging: fold another number's calls into this profile. Offered as a
        # picklist of everyone else, so it cannot be pointed at a number that
        # does not exist.
        others = []
        for row in history.callers():
            if row["key"] == key:
                continue
            other = saved.get(row["key"])
            name = (other.label if other else "") or history.suggested_name(row["key"])
            others.append(
                f'<option value="{_esc(row["key"])}">'
                f'{_esc(name or row["number"])} — {_esc(row["number"])}</option>'
            )
        merged = history.merged_into(key)
        merged_rows = "".join(
            '<div class="note"><div class="top">'
            f'<span>+{_esc(m)}</span>'
            '<span class="when">'
            '<form class="inline" method="post" action="/merge/undo">'
            f'<input type="hidden" name="source" value="{_esc(m)}">'
            f'<input type="hidden" name="back" value="{_esc(key)}">'
            '<button class="ghost">Separate</button></form></span></div></div>'
            for m in merged
        )
        merge_card = (
            '<div class="card"><div class="pad">'
            '<div class="muted" style="margin-bottom:10px">Fold another number\'s '
            "calls into this profile. The call log is not changed and this can be "
            "undone.</div>"
            '<form method="post" action="/merge">'
            f'<input type="hidden" name="target" value="{_esc(key)}">'
            '<label class="field"><span>Merge which number into this one?</span>'
            f'<select name="source">{"".join(others)}</select></label>'
            '<div class="actions"><button class="ghost">Merge</button></div></form>'
            "</div>" + merged_rows + "</div>"
            if others or merged else ""
        )

        danger = (
            '<div class="card"><div class="pad">'
            '<form method="post" action="/calls/delete-all" '
            "onsubmit=\"return confirm('Delete every call recorded for this "
            "caller? This cannot be undone.')\">"
            f'<input type="hidden" name="key" value="{_esc(key)}">'
            '<div class="muted" style="margin-bottom:10px">Deleting call history '
            "removes it from the agent's context as well as this page.</div>"
            '<button class="ghost">Delete all call history</button>'
            "</form></div></div>"
            if profile else ""
        )

        return _page(
            who,
            warn + head + "<h2>Contact</h2>" + form + "<h2>Your notes</h2>" + notes_card
            + "<h2>Calls</h2>"
            + ("".join(calls) or '<div class="card"><div class="empty">'
               "No calls yet.</div></div>")
            + ("<h2>Merge</h2>" + merge_card if merge_card else "")
            + ("<h2>Delete</h2>" + danger if danger else ""),
        )

    # -- merge / delete ----------------------------------------------------

    @admin.post("/merge")
    async def merge(
        request: Request, source: str = Form(""), target: str = Form("")
    ) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        if history.merge(source, target):
            log.info("merged one caller profile into another")
        return RedirectResponse(f"/caller/{target}", status_code=303)

    @admin.post("/merge/undo")
    async def merge_undo(
        request: Request, source: str = Form(""), back: str = Form("")
    ) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        history.unmerge(source)
        return RedirectResponse(f"/caller/{back}" if back else "/", status_code=303)

    @admin.post("/calls/delete")
    async def call_delete(
        request: Request, call_sid: str = Form(""), back: str = Form("")
    ) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        history.delete_call(call_sid)
        return RedirectResponse(f"/caller/{back}" if back else "/calls",
                                status_code=303)

    @admin.post("/calls/delete-all")
    async def calls_delete_all(request: Request, key: str = Form("")) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        history.delete_calls_for(key)
        return RedirectResponse(f"/caller/{key}", status_code=303)

    @admin.post("/notes/delete")
    async def note_delete(
        request: Request, key: str = Form(""), at: str = Form("")
    ) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        history.delete_note(key, at)
        return RedirectResponse(f"/caller/{key}", status_code=303)

    # -- calls -------------------------------------------------------------

    @admin.get("/calls")
    async def calls_page(request: Request) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)

        saved = _load()
        blocks = []
        for rec in history.recent(150):
            key = normalise(rec.get("from_number", ""), country_code)
            contact = saved.get(key)
            who = ((contact.display if contact else "")
                   or rec.get("caller_name") or "Unknown caller")
            gist = _clip(rec.get("summary") or rec.get("reason"), 150)
            inner = (
                f'<div style="margin-bottom:6px">{_direction(rec)}</div>'
                f'<div class="top"><span class="name">{_esc(who)}</span>'
                f'<span class="when">{_esc(_humanise_age(rec.get("started_at", "")))}</span></div>'
                + f'<div class="sub">{"to " if rec.get("direction") == "outbound" else ""}'
                + f'{_esc(rec.get("from_number") or "withheld")}</div>'
                + (f'<div class="body">{_esc(gist)}</div>' if gist else "")
                + f"<div>{_tags(rec)}</div>"
            )
            blocks.append(
                f'<a class="card" href="/caller/{_esc(key)}"><div class="pad">{inner}</div></a>'
                if key
                else f'<div class="card"><div class="pad">{inner}</div></div>'
            )
        return _page(
            "Calls",
            "".join(blocks) or '<div class="card"><div class="empty">No calls yet.</div></div>',
            here="calls",
        )

    # -- contacts ----------------------------------------------------------

    @admin.get("/contacts")
    async def contacts_redirect() -> Response:
        """Kept so old links and bookmarks still land somewhere sensible."""
        return RedirectResponse("/?only=contacts", status_code=303)

    @admin.post("/contacts/save")
    async def contact_save(
        request: Request,
        number: str = Form(""),
        name: str = Form(""),
        nickname: str = Form(""),
        relationship: str = Form(""),
        notes: str = Form(""),
        back: str = Form(""),
    ) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)

        target = normalise(number, country_code)
        name, nickname = name.strip(), nickname.strip()
        if not target or not (name or nickname):
            return RedirectResponse(
                f"/caller/{back}?err=1" if back else "/contacts?err=1", status_code=303
            )

        entries = _load()
        entries[target] = Contact(
            number=f"+{target}",
            name=name or nickname,
            nickname=nickname if nickname and nickname != name else "",
            relationship=relationship.strip(),
            notes=notes.strip(),
        )
        ok = _save(entries)
        log.info("admin saved contact %s", "ok" if ok else "FAILED")
        if back:
            return RedirectResponse(f"/caller/{back}{'' if ok else '?err=1'}",
                                    status_code=303)
        return RedirectResponse(f"/contacts{'' if ok else '?err=1'}", status_code=303)

    @admin.post("/contacts/delete")
    async def contact_delete(request: Request, number: str = Form("")) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        entries = _load()
        target = normalise(number, country_code)
        entries.pop(target, None)
        ok = _save(entries)
        return RedirectResponse(f"/contacts{'' if ok else '?err=1'}", status_code=303)

    @admin.post("/notes/add")
    async def note_add(
        request: Request, number: str = Form(""), text: str = Form("")
    ) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        history.add_note(number, text)
        key = normalise(number, country_code)
        return RedirectResponse(f"/caller/{key}" if key else "/", status_code=303)

    # -- settings ----------------------------------------------------------
    #
    # Only routing and behaviour are editable here. Secrets and safety flags
    # stay in `.env` and are shown as present/absent, never as values and never
    # as fields - see settings.py for the reasoning. A field that appears on
    # this page is one where the worst outcome of someone else editing it is
    # annoyance; a field that does not is one where the worst outcome is a bill
    # or a stranger's phone ringing.

    CHANNEL_LABELS = {
        "telegram": "Telegram",
        "email": "Email",
        "webhook": "Webhook",
        "whatsapp": "WhatsApp",
    }

    def _merged(name: str) -> dict:
        """A channel's settings entry, with env values showing through as defaults."""
        entry = cfg.settings.channel(name)
        defaults = {
            "telegram": {"chat_id": cfg.telegram_chat_id},
            "email": {
                "to": cfg.email_to,
                "host": cfg.smtp_host,
                "port": cfg.smtp_port,
                "username": cfg.smtp_username,
                "sender": cfg.smtp_sender,
                "starttls": cfg.smtp_starttls,
            },
            "webhook": {"url": cfg.webhook_url},
            "whatsapp": {
                "to": cfg.whatsapp_to,
                "bridge_url": cfg.whatsapp_bridge_url,
                "flavour": cfg.whatsapp_flavour,
                "session": cfg.whatsapp_session,
                "custom_path": "",
                "custom_body": "",
            },
        }[name]
        merged = dict(defaults)
        for key, value in entry.items():
            if value not in (None, ""):
                merged[key] = value
        # An entry that exists but has never been saved through the UI still
        # defaults to on, matching the pre-settings behaviour where a channel
        # with credentials was simply used.
        merged["enabled"] = entry.get("enabled", True) if entry else True
        return merged

    def _secret_pill(present: bool, var: str) -> str:
        if present:
            return f'<span class="pill on">{_esc(var)} set</span>'
        return f'<span class="pill off">{_esc(var)} missing</span>'

    def _field(label: str, name: str, value, hint: str = "", kind: str = "text") -> str:
        return (
            f'<label class="field"><span>{_esc(label)}</span>'
            f'<input type="{kind}" name="{_esc(name)}" value="{_esc(value)}"></label>'
            + (f'<p class="hint">{_esc(hint)}</p>' if hint else "")
        )

    def _checkbox(label: str, name: str, on: bool) -> str:
        checked = " checked" if on else ""
        return (
            f'<label class="chk"><input type="checkbox" name="{_esc(name)}"'
            f'{checked}><span>{_esc(label)}</span></label>'
        )

    def _channel_card(name: str, live: set[str]) -> str:
        values = _merged(name)
        on = name in live
        pill = (
            '<span class="pill on">delivering</span>'
            if on
            else '<span class="pill">not delivering</span>'
        )

        if name == "telegram":
            secret = _secret_pill(bool(cfg.telegram_bot_token), "TELEGRAM_BOT_TOKEN")
            body = (
                _field("Chat ID", "chat_id", values["chat_id"],
                       "The numeric chat the summary is posted to.")
                + f"<p>{secret}</p>"
                + '<p class="hint">The only channel that can trigger a callback: '
                  "a reply carries the id of the message it answers, and a "
                  "Telegram user id is not trivially forged.</p>"
            )
        elif name == "email":
            secret = _secret_pill(bool(cfg.smtp_password), "SMTP_PASSWORD")
            body = (
                _field("Send to", "to", values["to"])
                + '<div class="two">'
                + _field("SMTP host", "host", values["host"])
                + _field("Port", "port", values["port"])
                + "</div>"
                + '<div class="two">'
                + _field("Username", "username", values["username"])
                + _field("From address", "sender", values["sender"])
                + "</div>"
                + _checkbox("Use STARTTLS (off for implicit TLS on 465)",
                            "starttls", bool(values["starttls"]))
                + f"<p>{secret}</p>"
            )
        elif name == "webhook":
            secret = _secret_pill(
                bool(cfg.webhook_auth_header), "WEBHOOK_AUTH_HEADER"
            )
            body = (
                _field("URL", "url", values["url"],
                       "Receives the summary text plus the structured call record.")
                + f"<p>{secret} <span class=\"hint\">optional</span></p>"
            )
        else:
            secret = _secret_pill(
                bool(cfg.whatsapp_bridge_key), "WHATSAPP_BRIDGE_KEY"
            )
            options = "".join(
                f'<option value="{f}"{" selected" if values["flavour"] == f else ""}>'
                f"{f}</option>"
                for f in FLAVOURS
            )
            body = (
                _field("Send to", "to", values["to"], "Number in international form.")
                + _field("Bridge URL", "bridge_url", values["bridge_url"],
                         "Your self-hosted WhatsApp gateway, e.g. http://host.docker.internal:3000")
                + '<div class="two">'
                + '<label class="field"><span>Bridge type</span>'
                + f'<select name="flavour">{options}</select></label>'
                + _field("Session name", "session", values["session"])
                + "</div>"
                + _field("Custom send path", "custom_path", values["custom_path"],
                         "Only for bridge type 'custom'.")
                + '<label class="field"><span>Custom JSON body</span>'
                + '<textarea name="custom_body" placeholder=\'{"to": "{to}", "text": "{text}"}\'>'
                + _esc(values["custom_body"])
                + "</textarea></label>"
                + f"<p>{secret} <span class=\"hint\">optional</span></p>"
                + '<p class="hint">Pair a <b>dedicated</b> number, not your personal '
                  "account: the bridge can message anyone that account can reach.</p>"
                + '<p><a class="btn ghost" href="/settings/whatsapp">Pair a number</a></p>'
            )

        return (
            f'<form method="post" action="/settings/channel/{name}">'
            '<div class="card">'
            f'<div class="head"><span class="title">{CHANNEL_LABELS[name]}</span>'
            f'<span class="sp"></span>{pill}</div>'
            f'<div class="pad">'
            + _checkbox("Enabled", "enabled", bool(values["enabled"]))
            + body
            + '<div class="actions"><button>Save</button>'
            f'<button class="ghost" formaction="/settings/test/{name}">Send test</button>'
            "</div></div></div></form>"
        )

    def _routing_card(live: set[str]) -> str:
        names = [n for n in CHANNEL_NAMES if n in live] or list(CHANNEL_NAMES)
        header = "".join(f"<th>{CHANNEL_LABELS[n]}</th>" for n in names)

        def row(category: str, label: str, is_default: bool) -> str:
            current = cfg.settings.routing(category)
            # None means nothing configured, which the sender reads as "every
            # enabled channel" - so show that state as all-ticked rather than
            # as an empty row the reader would misread as "notifies nobody".
            selected = set(current) if current is not None else set(names)
            cells = "".join(
                f'<td><input type="checkbox" name="{category}:{n}"'
                f'{" checked" if n in selected else ""}></td>'
                for n in names
            )
            css = ' class="dflt"' if is_default else ""
            return f"<tr{css}><td>{_esc(label)}</td>{cells}</tr>"

        rows = row("default", "Everything else", True) + "".join(
            row(c, CATEGORY_LABELS.get(c, c), False) for c in ROUTING_CATEGORIES
        )
        return (
            '<form method="post" action="/settings/routing"><div class="card">'
            '<div class="head"><span class="title">Where each kind of call goes</span></div>'
            '<div class="pad">'
            '<p class="hint">Untick every box in a row to stop being told about '
            "that kind of call at all. The call is still recorded either way.</p>"
            '<div style="overflow-x:auto">'
            f'<table class="matrix"><thead><tr><th>Call type</th>{header}</tr></thead>'
            f"<tbody>{rows}</tbody></table></div>"
            '<div class="actions"><button>Save routing</button></div>'
            "</div></div></form>"
        )

    def _select(label: str, name: str, value, options, hint: str = "") -> str:
        """A dropdown, for anything with a knowably finite set of values.

        Preferred over a text box everywhere it is possible: a mistyped voice
        name or eagerness value does not fail here, it fails on a live call, and
        by then it is a caller hearing silence rather than a form showing an
        error. An unrecognised current value is preserved as an extra option so
        that saving an unrelated field cannot quietly rewrite it.
        """
        known = [str(v) for v, _ in options]
        current = "" if value is None else str(value)
        extra = (
            [(current, f"{current} (current, not a known value)")]
            if current and current not in known
            else []
        )
        rendered = "".join(
            f'<option value="{_esc(v)}"{" selected" if str(v) == current else ""}>'
            f"{_esc(text)}</option>"
            for v, text in list(options) + extra
        )
        return (
            f'<label class="field"><span>{_esc(label)}</span>'
            f'<select name="{_esc(name)}">{rendered}</select></label>'
            + (f'<p class="hint">{_esc(hint)}</p>' if hint else "")
        )

    def _voice_fields() -> str:
        """The provider-specific knobs, for whichever provider is running.

        Showing both sets at once would be worse than showing the wrong one:
        every deployment would have boxes that do nothing, and no way to tell
        which. Choosing the provider, and everything credential-shaped, lives in
        the setup wizard - this page is for tuning one that already works.
        """
        if cfg.voice_provider == "elevenlabs":
            return (
                _select("Language", "elevenlabs_language", cfg.elevenlabs_language,
                        [("", "Use whatever the agent is set to")]
                        + CHOICES["elevenlabs_language"])
                + '<p class="hint">Voice, agent and turn-taking belong to '
                + f"ElevenLabs agent <code>{_esc(cfg.elevenlabs_agent_id or 'unset')}"
                + '</code>. Pick them in the <a href="/setup/voice">setup wizard</a>, '
                + "which reads the live list from your account.</p>"
            )
        return (
            '<div class="two">'
            + _select("Voice", "openai_voice", cfg.openai_voice,
                      CHOICES["openai_voice"])
            + _select("Turn-taking", "vad_eagerness", cfg.vad_eagerness,
                      CHOICES["vad_eagerness"])
            + "</div>"
            + _select("Model", "openai_realtime_model", cfg.openai_realtime_model,
                      CHOICES["openai_realtime_model"])
        )

    def _behaviour_card() -> str:
        return (
            '<form method="post" action="/settings/behaviour"><div class="card">'
            '<div class="head"><span class="title">How it handles a call</span>'
            f'<span class="pill">{_esc(cfg.voice_provider)}</span></div>'
            '<div class="pad">'
            + '<label class="field"><span>Greeting (spoken verbatim to an unknown caller)</span>'
            + f'<textarea name="greeting">{_esc(cfg.greeting)}</textarea></label>'
            + _voice_fields()
            + '<div class="two">'
            + _field("Wrap up after (seconds)", "wrap_up_after_s", cfg.wrap_up_after_s)
            + _field("Wrap up after (caller turns)", "wrap_up_after_turns",
                     cfg.wrap_up_after_turns)
            + "</div>"
            + '<div class="two">'
            + _field("Max call length (seconds)", "max_call_seconds",
                     cfg.max_call_seconds)
            + _field("Hang up after silence (seconds)", "silence_hangup_seconds",
                     cfg.silence_hangup_seconds)
            + "</div>"
            + _checkbox("Tell the agent about previous calls from the same number",
                        "history_enabled", bool(cfg.history_enabled))
            + _field("How many previous calls to show", "history_max_calls",
                     cfg.history_max_calls)
            + '<div class="actions"><button>Save</button></div>'
            "</div></div></form>"
        )

    def _wizard_card() -> str:
        """The way in to first-time setup, from the page people already open."""
        provider = cfg.voice_provider
        ready, why = cfg.provider_ready(provider)
        state = (
            f'<span class="pill on">{_esc(provider)}</span>'
            if ready
            else f'<span class="pill off">{_esc(provider)} — {_esc(why)}</span>'
        )
        return (
            '<div class="card"><div class="head"><span class="title">Setup wizard'
            f"</span>{state}</div><div class=pad>"
            '<p class="hint">Voice provider, API keys, and provisioning the '
            "ElevenLabs agent — with the values that have a knowable set offered "
            "as dropdowns rather than boxes to mistype.</p>"
            '<div class="actions"><a class="btn" href="/setup">Open setup wizard'
            "</a></div></div></div>"
        )

    @admin.get("/settings")
    async def settings_page(request: Request, saved: str = "", msg: str = "") -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        if cfg is None or notifier is None:
            return _page("Settings", '<div class="empty">Settings are not available '
                         "in this build.</div>", here="settings")

        live = set(notifier.channels())
        banner = ""
        if msg:
            banner = f'<p class="ok-msg">{_esc(msg)}</p>'
        elif saved:
            banner = '<p class="ok-msg">Saved. It applies to the next call.</p>'

        warn = ""
        if not live:
            warn = (
                '<div class="card"><div class="pad"><b>No channel is delivering.</b>'
                '<p class="hint">Calls are still answered and recorded, but nobody '
                "is told about them. Enable a channel below.</p></div></div>"
            )

        cards = "".join(_channel_card(n, live) for n in CHANNEL_NAMES)
        return _page(
            "Settings",
            banner + warn
            + "<h2>Setup</h2>" + _wizard_card()
            + "<h2>Channels</h2>" + cards
            + "<h2>Routing</h2>" + _routing_card(live)
            + "<h2>Call handling</h2>" + _behaviour_card(),
            here="settings",
        )

    def _persist(mutate) -> str:
        """Apply a change to the settings file. Returns "" or an error message."""
        data = cfg.settings.raw()
        mutate(data)
        if not cfg.settings.save(data):
            return "Could not write settings - is ./config mounted read-only?"
        return ""

    @admin.post("/settings/channel/{name}")
    async def save_channel(request: Request, name: str) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        if cfg is None or name not in CHANNEL_NAMES:
            return RedirectResponse("/settings", status_code=303)

        form = await request.form()
        entry: dict = {"enabled": form.get("enabled") is not None}
        for key in form:
            if key == "enabled":
                continue
            value = str(form.get(key) or "").strip()
            if key == "port":
                try:
                    entry[key] = int(value or 587)
                except ValueError:
                    entry[key] = 587
            else:
                entry[key] = value
        if name == "email":
            entry["starttls"] = form.get("starttls") is not None

        def mutate(data: dict) -> None:
            data.setdefault("channels", {})[name] = entry

        error = _persist(mutate)
        log.info("admin updated the %s channel", name)
        return RedirectResponse(
            f"/settings?saved=1&msg={quote_plus(error)}" if error else "/settings?saved=1",
            status_code=303,
        )

    @admin.post("/settings/routing")
    async def save_routing(request: Request) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        if cfg is None:
            return RedirectResponse("/settings", status_code=303)

        form = await request.form()
        table: dict[str, list[str]] = {}
        for category in ("default",) + ROUTING_CATEGORIES:
            table[category] = [
                n for n in CHANNEL_NAMES if form.get(f"{category}:{n}") is not None
            ]

        error = _persist(lambda data: data.__setitem__("routing", table))
        log.info("admin updated notification routing")
        return RedirectResponse(
            f"/settings?saved=1&msg={quote_plus(error)}" if error else "/settings?saved=1",
            status_code=303,
        )

    @admin.post("/settings/behaviour")
    async def save_behaviour(request: Request) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        if cfg is None:
            return RedirectResponse("/settings", status_code=303)

        form = await request.form()
        # Seeded from what is already saved rather than started empty, because
        # this form no longer renders every behaviour key: the voice fields are
        # whichever pair matches VOICE_PROVIDER. Replacing the block wholesale
        # would silently wipe the other provider's settings every time this page
        # was saved, and only show up on the day someone switched provider back.
        values: dict = dict(
            (cfg.settings.raw().get("behaviour") or {})
            if cfg is not None
            else {}
        )
        for key, want in BEHAVIOUR_KEYS.items():
            if want is bool:
                # Every bool is rendered on every variant of this form, so an
                # absent checkbox really does mean unticked.
                values[key] = form.get(key) is not None
                continue
            if key not in form:
                continue
            raw = str(form.get(key) or "").strip()
            if want is int:
                try:
                    values[key] = int(raw)
                except ValueError:
                    # Leave the previous value rather than writing a broken one.
                    continue
            elif key in CHOICES and raw and raw not in dict(CHOICES[key]):
                # The form renders these as dropdowns, so this only fires on a
                # hand-crafted POST. Rejected rather than stored: an unknown
                # voice or eagerness value does not fail here, it fails on a live
                # call as silence.
                log.warning("admin sent %s=%r, which is not a known value", key, raw)
                continue
            else:
                values[key] = raw

        error = _persist(lambda data: data.__setitem__("behaviour", values))
        log.info("admin updated call handling settings")
        return RedirectResponse(
            f"/settings?saved=1&msg={quote_plus(error)}" if error else "/settings?saved=1",
            status_code=303,
        )

    @admin.post("/settings/test/{name}")
    async def test_channel(request: Request, name: str) -> Response:
        """Send a test message down one channel, without saving the form."""
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        if cfg is None or notifier is None or name not in CHANNEL_NAMES:
            return RedirectResponse("/settings", status_code=303)

        built = notifier.channels().get(name)
        if built is None:
            msg = f"{CHANNEL_LABELS[name]} is not enabled and configured yet."
        else:
            ref = await built.send(
                "📞 Test message from your receptionist. If you can read this, "
                f"{CHANNEL_LABELS[name]} is working."
            )
            msg = (
                f"Sent a test to {CHANNEL_LABELS[name]}."
                if ref is not None
                else f"{CHANNEL_LABELS[name]} failed - check the container log."
            )
        return RedirectResponse(f"/settings?msg={quote_plus(msg)}", status_code=303)

    # -- WhatsApp pairing --------------------------------------------------

    @admin.get("/settings/whatsapp")
    async def whatsapp_pair(request: Request, msg: str = "") -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        if cfg is None:
            return RedirectResponse("/settings", status_code=303)

        bridge = build_bridge(cfg)
        if not bridge.configured:
            return _page(
                "Pair WhatsApp",
                '<div class="card"><div class="pad"><b>No bridge URL set.</b>'
                '<p class="hint">Add one under Settings first.</p>'
                '<p><a class="btn ghost" href="/settings">Back to settings</a></p>'
                "</div></div>",
                here="settings",
            )

        state, detail = await bridge.status()
        body = ""

        if state == "connected":
            body = (
                '<div class="card"><div class="head"><span class="title">Paired</span>'
                '<span class="sp"></span><span class="pill on">connected</span></div>'
                '<div class="pad"><p>This bridge is signed in and ready to send.</p>'
                '<p><a class="btn ghost" href="/settings">Back to settings</a></p>'
                "</div></div>"
            )
        else:
            data_url, error = await bridge.qr()
            if data_url:
                body = (
                    '<div class="card"><div class="head">'
                    '<span class="title">Scan to pair</span><span class="sp"></span>'
                    f'<span class="pill">{_esc(detail or state)}</span></div>'
                    f'<div class="pad"><div class="qr"><img src="{data_url}" '
                    'alt="WhatsApp pairing QR code"></div>'
                    '<p class="hint">On the phone holding the <b>dedicated</b> number: '
                    "WhatsApp → Settings → Linked devices → Link a device. "
                    "This page refreshes itself until the bridge reports connected.</p>"
                    "</div></div>"
                )
            else:
                body = (
                    '<div class="card"><div class="head">'
                    '<span class="title">Not paired</span><span class="sp"></span>'
                    f'<span class="pill off">{_esc(state)}</span></div>'
                    f'<div class="pad"><p class="hint">{_esc(error or detail)}</p>'
                    '<form method="post" action="/settings/whatsapp/start">'
                    '<div class="actions"><button>Start session</button>'
                    '<a class="btn ghost" href="/settings/whatsapp">Refresh</a>'
                    "</div></form></div></div>"
                )

        # A meta refresh rather than a script: the rest of this UI is plain
        # forms with no JavaScript, and a pairing page is exactly where a
        # locked-down browser should still work.
        refresh = "" if state == "connected" else '<meta http-equiv="refresh" content="6">'
        page = _page(
            "Pair WhatsApp",
            (f'<p class="ok-msg">{_esc(msg)}</p>' if msg else "") + body,
            here="settings",
        )
        return HTMLResponse(page.body.decode().replace("</head>", refresh + "</head>"))

    @admin.post("/settings/whatsapp/start")
    async def whatsapp_start(request: Request) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        if cfg is None:
            return RedirectResponse("/settings", status_code=303)
        ok, detail = await build_bridge(cfg).start()
        msg = "Starting the session - a QR should appear shortly." if ok else detail
        return RedirectResponse(
            f"/settings/whatsapp?msg={quote_plus(msg)}", status_code=303
        )

    @admin.get("/health")
    async def admin_health() -> dict:
        return {"status": "ok", "surface": "admin"}

    # The setup wizard, in its own module because this one is long enough. It is
    # handed the helpers rather than importing them, so the auth check, the HTML
    # escaping and the page shell stay defined exactly once - a second copy of
    # any of those is how an admin UI grows a hole.
    if cfg is not None and secrets is not None:
        from .wizard import register as register_wizard

        register_wizard(
            admin,
            {
                "cfg": cfg,
                "secrets": secrets,
                "notifier": notifier,
                "persist": _persist,
                "authed": _authed,
                "page": _page,
                "esc": _esc,
            },
        )

    return admin
