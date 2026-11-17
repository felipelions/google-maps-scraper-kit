#!/usr/bin/env python3
"""The kit's own engine: the Google Maps scraper's HTTP API and web UI, on this machine only.

    python3 engine/serve.py               # port from SCRAPER_PORT (environment, then .env), default 8080
    python3 engine/serve.py --port 8190

Normally started by `python3 scripts/engine.py start`, or by `docker compose up -d` (the Dockerfile). It speaks the API of the engine the kit used
before (POST/GET/DELETE /api/v1/jobs, /api/v1/jobs/{id}/download, the same job body and "Status"
values), so scripts/scrape.py, the skill and the slash commands work unchanged; jobs also carry Error
and Progress, resume after a restart, and download as CSV or JSON. It listens on
127.0.0.1 (and ::1) only, or on 0.0.0.0 inside the Docker image, where Docker publishes the port on
the host's 127.0.0.1 only. Either way it refuses a foreign Host (DNS rebinding, 421) or Origin (cross-site
posts, 403); with SCRAPER_API_KEY set it also wants that X-API-Key (401). No telemetry: it talks to
Google Maps, for email or socials jobs to the businesses' own websites, and for verify_emails jobs
to Google's public DNS (dns.google), nothing else.
Standard library only.
"""
import argparse, csv, gzip, hmac, html, io, json, os, re, secrets, signal, socket, socketserver, sqlite3, sys, threading, time, unicodedata, zlib
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser, jobs  # noqa: E402

for _stream in (sys.stdout, sys.stderr):  # ✓ and accented names on a Windows cp1252 console
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

VERSION = "2.5.1"
MARK = f"gmskit-engine/{VERSION}"  # X-Engine header: tells scripts/engine.py this engine from others
BRAND = "GEO API - Big data"
REPO = "https://github.com/Mahanaicoach/google-maps-scraper-kit"
# The web UI's update notice: the browser asks GitHub for the kit's latest release once a day (a
# public JSON endpoint, nothing about the user in the request) and shows a line when it is newer
# than this copy. Nothing updates itself: the line says to run scripts/update.sh.
RELEASES = "https://api.github.com/repos/Mahanaicoach/google-maps-scraper-kit/releases/latest"
# The web UI's What and Where boxes offer only these, so nobody searches for a business type or a
# place that does not exist: presets/business-types.json (hand-picked; add a niche there) and
# presets/cities.tsv.gz (GeoNames cities1000, CC BY 4.0: name, region, country code, lat, lon, biggest first).
# What also offers Google Business Profile's category names (4,053, US English) from
# lobstrio/google-business-categories, pinned to one commit. The browser loads that list from GitHub;
# the kit ships no copy, because that repository has no licence file.
CATEGORIES = ("https://raw.githubusercontent.com/lobstrio/google-business-categories/"
              "4ee1c4788d07c812a32986177c2a51c28957062f/data/google-business-categories.txt")
UI_COOKIE = "gmskit_ui"
UI_TTL = 7 * 24 * 3600  # session cookie lifetime in seconds
PRESETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets")


def load_types(folder=None):
    """The What list; [] with one warning when the file is missing or broken, so the API and the scripts
    still work (only the web UI's What box is empty)."""
    try:
        with open(os.path.join(folder or PRESETS, "business-types.json"), encoding="utf-8") as f:
            types = json.load(f)
        if not isinstance(types, list) or not all(isinstance(t, str) for t in types):
            raise ValueError("not a list of names")
        return types
    except (OSError, ValueError) as e:
        print(f"presets/business-types.json unusable ({e.__class__.__name__}): the What box will be empty", file=sys.stderr)
        return []


TYPES = load_types()
TYPES_JS = json.dumps(TYPES).replace("</", "<\\/")  # the What list, inlined in the page script
_cities, _cities_lock = None, threading.Lock()
ENV_KEYS = ("SCRAPER_PORT", "SCRAPER_API_KEY", "SCRAPER_UI_PASSWORD", "SCRAPER_ALLOWED_HOSTS", "SCRAPER_HEADLESS")
MAX_BODY = 1 << 20
LOCAL = r"(localhost|127\.0\.0\.1|\[::1\])"
HOST_OK = re.compile(rf"^{LOCAL}(:\d{{1,5}})?$", re.I)
ORIGIN_OK = re.compile(rf"^https?://{LOCAL}(:\d{{1,5}})?$", re.I)
# Extra host names the engine answers to when it runs behind a reverse proxy (Easypanel, Traefik, nginx),
# comma separated, without scheme or port. Default: none, so only localhost. Reloaded from .env in main().
ALLOWED_HOSTS = {h.strip().lower() for h in os.environ.get("SCRAPER_ALLOWED_HOSTS", "").split(",") if h.strip()}


def set_allowed_hosts(hosts_csv):
    """Replace ALLOWED_HOSTS from SCRAPER_ALLOWED_HOSTS (environment or .env)."""
    global ALLOWED_HOSTS
    ALLOWED_HOSTS = {h.strip().lower() for h in (hosts_csv or "").split(",") if h.strip()}


def host_name(host):
    """Hostname without port (and without brackets noise for comparison)."""
    return (host or "").lower().rsplit(":", 1)[0]


def host_allowed(host):
    return bool(HOST_OK.match(host)) or host_name(host) in ALLOWED_HOSTS if host else False


def origin_allowed(origin, host):
    """Allow localhost origins matching Host, same-host Origins, or Origins in SCRAPER_ALLOWED_HOSTS.

    Behind Traefik/Easypanel the browser sends Origin: https://public.domain while Host may still be
    localhost or the public name — requiring Origin host == Host broke the login POST with 403."""
    if ORIGIN_OK.match(origin) and origin.split("://", 1)[1].lower() == host.lower():
        return True
    scheme, _, rest = origin.partition("://")
    if scheme.lower() not in ("http", "https") or not rest:
        return False
    origin_host = host_name(rest)
    if origin_host == host_name(host):
        return True
    return origin_host in ALLOWED_HOSTS


def ui_token(password, exp):
    """Signed session value for the web UI login cookie."""
    sig = hmac.new(password.encode(), f"gmskit_ui|{exp}".encode(), "sha256").hexdigest()
    return f"{exp}.{sig}"


def parse_cookies(header):
    out = {}
    for part in (header or "").split(";"):
        k, sep, v = part.strip().partition("=")
        if sep:
            out[k] = v
    return out


def ui_session_ok(password, cookie_header):
    """True only with a valid gmskit_ui cookie for the configured SCRAPER_UI_PASSWORD.
    Without a password configured, the web UI stays locked."""
    if not password:
        return False
    raw = parse_cookies(cookie_header).get(UI_COOKIE, "")
    exp_s, sep, sig = raw.partition(".")
    if not sep or not exp_s.isdigit() or not sig:
        return False
    exp = int(exp_s)
    if exp < int(time.time()):
        return False
    want = hmac.new(password.encode(), f"gmskit_ui|{exp}".encode(), "sha256").hexdigest()
    return hmac.compare_digest(want, sig)


def cookie_header(value, max_age, secure=False):
    """Set-Cookie for gmskit_ui, or a clearing cookie when value is empty."""
    parts = [f"{UI_COOKIE}={value}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


JOB_PATH = re.compile(r"^/api/v1/jobs/([0-9a-f-]{36})(/download)?$")
JOB_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_log_lock = threading.Lock()
_REPLIED = object()  # body() answered the request itself (a bad body)


def file_name(job, jid):
    """"bakery-in-portland-or-c063a815": the job's search, then the start of its id; the web UI names
    its downloads the same way (fileName in PAGE)."""
    s = unicodedata.normalize("NFKD", (job or {}).get("Name") or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return (re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:60] or "results") + "-" + jid[:8]


FOLD = str.maketrans({"ł": "l", "ø": "o", "đ": "d", "ı": "i", "ß": "ss", "æ": "ae", "œ": "oe", "þ": "th"})


def fold(s):
    """"sao paulo sp br" for "São Paulo, SP, BR": accents dropped (also ł, ø, ı...), lower case, punctuation
    as single spaces."""
    s = "".join(c for c in unicodedata.normalize("NFKD", s.lower().translate(FOLD)) if not unicodedata.combining(c))
    return " ".join(re.sub(r"[\W_]+", " ", s).split())


def cities():
    """Every city as one "folded label<TAB>label<TAB>lat<TAB>lon<TAB>country" line, biggest first. Read on the
    first lookup, then kept (about 35 MB)."""
    global _cities
    with _cities_lock:
        if _cities is None:
            rows = []
            try:
                with gzip.open(os.path.join(PRESETS, "cities.tsv.gz"), "rt", encoding="utf-8") as f:
                    for line in f:
                        name, admin, cc, lat, lon = line.rstrip("\n").split("\t")
                        label = ", ".join([name] + ([admin] if admin and admin != name else []) + [cc])
                        rows.append("\t".join((fold(label), label, lat, lon, cc)))
            except (OSError, EOFError, ValueError, zlib.error) as e:  # missing, truncated, corrupt or odd file: what was read, once
                log(f"presets/cities.tsv.gz unusable after {len(rows)} cities ({e.__class__.__name__})")
            _cities = rows
    return _cities


def find_cities(q, limit=20):
    """The biggest cities whose "name, region, country" starts with q; with no q, the biggest in the US."""
    raw, q, out = q.strip(), fold(q), []
    if raw and not q:  # punctuation only: nothing can start with it
        return out
    for row in cities():  # ponytail: a linear scan, about 30 ms; a prefix index if lookups ever lag
        if row.startswith(q) if q else row.endswith("\tUS"):
            _, label, lat, lon, cc = row.split("\t")
            out.append({"label": label, "lat": lat, "lon": lon, "country": cc})
            if len(out) == limit:
                break
    return out


def log(msg):
    """One line on stderr with a UTC timestamp and its offset. Counts only, never scraped values.
    A log that cannot be written (full disk, closed stderr) is dropped, never raised."""
    with _log_lock:
        try:
            print(f"{jobs.utc_now()} {msg}", file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass


def env_value(v):
    """A .env value: quotes stripped, a trailing " # comment" dropped unless it sits inside the quotes
    (then docker compose keeps it too). Same rule as scripts/scrape.py."""
    v = v.strip()
    if v[:1] in ("\"", "'") and v[0] in v[1:]:
        return v[1:v.index(v[0], 1)]
    return v.split(" #", 1)[0].strip()


def read_env():
    """SCRAPER_PORT, SCRAPER_API_KEY, SCRAPER_UI_PASSWORD, SCRAPER_ALLOWED_HOSTS and SCRAPER_HEADLESS:
    the environment wins, then the kit's .env (last line wins, as in docker compose and scripts/scrape.py)."""
    found = {}
    try:
        with open(os.path.join(browser.KIT, ".env"), encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                k, sep, v = line.strip().removeprefix("export ").partition("=")
                if sep and k.strip() in ENV_KEYS:
                    found[k.strip()] = env_value(v)
    except OSError:
        pass
    return {k: os.environ.get(k, found.get(k, "")) for k in ENV_KEYS}


JOB_PROPS = {
    "name": {"type": "string", "description": "any label"},
    "keywords": {"type": "array", "items": {"type": "string"}, "description": "searches, run one after another"},
    "lang": {"type": "string", "description": "2-letter interface language, e.g. en (category names, hours text)"},
    "country": {"type": "string", "description": "2-letter country Google ranks and formats results for, e.g. us "
                                                 "(default: the country of this machine's IP)"},
    "lat": {"type": "string", "description": "the map's centre, e.g. \"30.2672\"; send lat and lon both, or neither "
                                             "(then Google places the search by the keyword, e.g. \"cafes in Austin TX\")"},
    "lon": {"type": "string", "description": "e.g. \"-97.7431\""},
    "max_time": {"type": "integer", "description": "seconds"},
    "depth": {"type": "integer", "description": "result batches per keyword, about 20 places each (default 1)"},
    "zoom": {"type": "integer", "description": "map zoom 1-21 (default 15)"},
    "email": {"type": "boolean", "description": "visit each website's homepage for emails"},
    "verify_emails": {"type": "boolean", "description": "with email: check each address's domain over DNS (dns.google), "
                                                       "drop domains that do not exist, add email_host"},
    "browser_sites": {"type": "boolean", "description": "optional Playwright retry when HTTP finds no email or social links; adds website_fetch_status"},
    "socials": {"type": "boolean", "description": "visit each website's homepage for Instagram, Facebook, LinkedIn, "
                                                 "TikTok, YouTube and X links"},
    "reviews_max": {"type": "integer", "description": "0-5 reviews per place from its panel, one page load per place (headless supported)"},
    "proxies": {"type": "array", "items": {"type": "string"},
                "description": "http(s)://[user:pass@]host:port or socks5://host:port; one per keyword in turn, "
                               "and the next one when Google refuses a keyword"},
    "radius": {"type": "integer", "description": "accepted, not used"},
    "fast_mode": {"type": "boolean", "description": "accepted, not used"},
}
OPENAPI = {
    "openapi": "3.0.3",
    "info": {"title": "Google Maps Scraper Kit engine", "version": VERSION,
             "description": "Local only. Create a job, poll its Status until ok or failed (Progress says how far "
                            "it is), download the CSV or JSON. A job the engine was stopped in the middle of "
                            f"resumes from its last finished keyword on the next start. Source and docs: {REPO}"},
    "paths": {
        "/api/v1/jobs": {
            "get": {"summary": "List jobs, newest first (null when there are none)"},
            "post": {"summary": "Create a job; returns 201 {\"id\": \"<uuid>\"}, or 422 {\"code\", \"message\"}",
                     "requestBody": {"content": {"application/json": {"schema": {
                         "type": "object", "required": ["name", "keywords", "lang", "max_time"],
                         "properties": JOB_PROPS}}}}}},
        "/api/v1/jobs/{id}": {
            "get": {"summary": "One job: ID, Name, Date, Status (pending, working, ok, failed), Data (proxy "
                               "passwords masked), Error, Progress (keywords_done, keywords_total, places, percent, "
                               "stage: queued, search, reviews, websites or done)"},
            "delete": {"summary": "Delete a job and its results (stops it if it is running)"}},
        "/api/v1/jobs/{id}/download": {"get": {"summary": "The job's CSV, the rows so far while it works; "
                                                          "?format=leads for the 8 lead fields and socials with junk "
                                                          "emails dropped, as scripts/scrape.py writes them; "
                                                          "?format=json for a JSON array of the same rows"}},
        "/api/v1/cities": {"get": {"summary": "Up to 20 cities for the web UI's Where box, each {label, lat, lon, "
                                              "country}: ?q=aus gives the biggest whose \"name, region, country\" "
                                              "starts with it, no q the biggest US cities (GeoNames, CC BY 4.0)"}},
        "/api/openapi.json": {"get": {"summary": "This description as OpenAPI JSON (the page at /api/docs shows it)"}},
    },
}


def docs_page(nonce):
    """/api/docs: OPENAPI as a plain HTML page (no scripts, nothing loaded from elsewhere)."""
    e = html.escape
    out = [f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
           "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>Engine API</title>"
           f"<style nonce=\"{nonce}\">:root {{ color-scheme: light dark; }} body {{ margin: 0 auto; max-width: 960px; "
           "padding: 16px; font: 15px/1.45 system-ui, -apple-system, Segoe UI, sans-serif; }} code {{ font-size: .95em; }} "
           "table {{ width: 100%; border-collapse: collapse; }} th, td {{ text-align: left; vertical-align: top; "
           "padding: 4px 6px; border-bottom: 1px solid #8884; }} .m {{ font-weight: 600; }}</style></head><body>",
           f"<h1>{e(OPENAPI['info']['title'])} {e(VERSION)}</h1><p>{e(OPENAPI['info']['description'])}</p>",
           "<p><a href=\"/\">Web UI</a> · <a href=\"/api/openapi.json\">OpenAPI JSON</a> · "
           "With SCRAPER_API_KEY set, send it as an <code>X-API-Key</code> header.</p>"]
    for path, ops in OPENAPI["paths"].items():
        for method, op in ops.items():
            out.append(f"<h2><span class=\"m\">{e(method.upper())}</span> <code>{e(path)}</code></h2><p>{e(op['summary'])}</p>")
            schema = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
            if schema:
                out.append("<table><tr><th>Field</th><th>Type</th><th>Required</th><th>Meaning</th></tr>")
                for name, prop in schema["properties"].items():
                    out.append(f"<tr><td><code>{e(name)}</code></td><td>{e(prop['type'])}</td>"
                               f"<td>{'yes' if name in schema['required'] else ''}</td>"
                               f"<td>{e(prop.get('description', ''))}</td></tr>")
                out.append("</table>")
    out.append("</body></html>")
    return "".join(out)


LOGIN = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Entrar — {brand}</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 24 24%27%3E%3Cpath fill=%27%23000%27 d=%27M12 2a7 7 0 0 0-7 7c0 5.2 7 13 7 13s7-7.8 7-13a7 7 0 0 0-7-7zm0 9.5a2.5 2.5 0 1 1 0-5 2.5 2.5 0 0 1 0 5z%27/%3E%3C/svg%3E">
<style nonce="{nonce}">
:root {{ color-scheme: light dark; --bg: #fafafa; --fg: #171717; --muted: #737373; --border: #e5e5e5;
  --card: #fff; --btn: #171717; --btn-fg: #fafafa; --bad: #dc2626; --ring: #171717;
  --font: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #0a0a0a; --fg: #ededed; --muted: #a3a3a3; --border: #262626;
  --card: #111; --btn: #ededed; --btn-fg: #0a0a0a; --bad: #f87171; --ring: #ededed; }} }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px;
  background: var(--bg); color: var(--fg); font: 15px/1.5 var(--font); -webkit-font-smoothing: antialiased; }}
.card {{ width: min(380px, 100%); padding: 32px; background: var(--card); border: 1px solid var(--border); border-radius: 12px; }}
h1 {{ margin: 0 0 6px; font-size: 1.25rem; font-weight: 600; letter-spacing: -.02em; }}
.sub {{ margin: 0 0 24px; color: var(--muted); font-size: .9rem; }}
label {{ display: grid; gap: 8px; font-size: .85rem; font-weight: 500; }}
input {{ font: inherit; width: 100%; min-height: 42px; padding: 10px 12px; color: var(--fg); background: var(--bg);
  border: 1px solid var(--border); border-radius: 8px; }}
input:focus {{ outline: 2px solid var(--ring); outline-offset: 1px; border-color: transparent; }}
button {{ margin-top: 16px; width: 100%; min-height: 42px; padding: 10px 16px; border: 0; border-radius: 8px;
  background: var(--btn); color: var(--btn-fg); font: inherit; font-weight: 550; cursor: pointer; }}
button:hover {{ filter: brightness(1.08); }}
.err {{ margin: 12px 0 0; color: var(--bad); font-size: .875rem; min-height: 1.25em; }}
</style></head><body>
<form class="card" method="post" action="/login" id="f">
  <h1>{brand}</h1>
  <p class="sub">Acesso restrito. Digite a senha para entrar.</p>
  <label for="password">Senha de acesso
    <input id="password" name="password" type="password" autocomplete="current-password" required autofocus {disabled}>
  </label>
  <button type="submit" {disabled}>Entrar</button>
  <p class="err" id="err" role="status">{error}</p>
</form>
</body></html>"""


PAGE = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{brand}</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 24 24%27%3E%3Cpath fill=%27%23000%27 d=%27M12 2a7 7 0 0 0-7 7c0 5.2 7 13 7 13s7-7.8 7-13a7 7 0 0 0-7-7zm0 9.5a2.5 2.5 0 1 1 0-5 2.5 2.5 0 0 1 0 5z%27/%3E%3C/svg%3E">
<style nonce="{nonce}">
:root {{ color-scheme: light dark; --bg: #fafafa; --fg: #171717; --muted: #737373; --border: #e5e5e5;
  --card: #fff; --btn: #171717; --btn-fg: #fafafa; --soft-btn: #f5f5f5; --bad: #dc2626; --ok: #16a34a;
  --warn: #ca8a04; --ring: #171717; --hover: #f5f5f5;
  --font: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #0a0a0a; --fg: #ededed; --muted: #a3a3a3; --border: #262626;
  --card: #111; --btn: #ededed; --btn-fg: #0a0a0a; --soft-btn: #1a1a1a; --bad: #f87171; --ok: #4ade80;
  --warn: #facc15; --ring: #ededed; --hover: #1a1a1a; }} }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.5 var(--font); -webkit-font-smoothing: antialiased; }}
main {{ max-width: 880px; margin: 0 auto; padding: 0 20px 72px; }}
a {{ color: inherit; text-underline-offset: 3px; }} a:hover {{ opacity: .8; }}
.top {{ display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px 20px;
  padding: 24px 0; border-bottom: 1px solid var(--border); }}
h1 {{ margin: 0; font-size: 1.15rem; font-weight: 600; letter-spacing: -.02em; }}
.top nav {{ display: flex; align-items: center; gap: 8px 14px; font-size: .875rem; color: var(--muted); }}
.top nav a, .top nav button.link {{ color: var(--muted); text-decoration: none; background: none; border: 0;
  padding: 0; min-height: 0; font: inherit; cursor: pointer; }}
.top nav a:hover, .top nav button.link:hover {{ color: var(--fg); }}
.mono {{ font-family: var(--mono); font-size: .8em; }} .soft {{ color: var(--muted); font-size: .875rem; }}
.update {{ margin-top: 16px; padding: 12px 14px; border: 1px solid var(--border); border-radius: 10px;
  background: var(--card); font-size: .875rem; }}
.update code {{ font-size: .8rem; font-family: var(--mono); }}
h2 {{ margin: 0 0 14px; font-size: 1rem; font-weight: 600; letter-spacing: -.01em; }}
section {{ padding: 32px 0 0; }}
form {{ display: grid; gap: 14px; }}
.ww {{ display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }}
.pick {{ display: grid; gap: 6px; align-content: start; }}
.search {{ display: flex; align-items: center; gap: 10px; padding: 8px 12px; background: var(--card);
  border: 1px solid var(--border); border-radius: 8px; }}
.search:focus-within {{ outline: 2px solid var(--ring); outline-offset: 1px; border-color: transparent; }}
.search svg {{ flex: none; color: var(--muted); }}
.search input {{ flex: 1; padding: 0; border: 0; outline: 0; background: transparent; font-size: 1rem; color: var(--fg); }}
.box {{ position: relative; }}
.opts {{ position: absolute; z-index: 5; left: 0; right: 0; top: calc(100% + 4px); max-height: 280px; overflow-y: auto;
  margin: 0; padding: 4px; list-style: none; background: var(--card); border: 1px solid var(--border); border-radius: 8px;
  box-shadow: 0 8px 24px rgb(0 0 0 / .12); }}
.opts li {{ padding: 9px 10px; border-radius: 6px; cursor: pointer; }}
.opts li:hover, .opts li[aria-selected=true] {{ background: var(--hover); }}
.tags:empty {{ display: none; }}
.tag {{ min-height: 32px; padding: 4px 10px; border: 1px solid var(--border); border-radius: 999px;
  background: var(--soft-btn); font-size: .85rem; }}
.hint {{ margin: -4px 2px 0; }}
label {{ display: grid; gap: 6px; font-size: .875rem; font-weight: 500; }}
input {{ font: inherit; font-weight: 400; min-height: 40px; width: 100%; padding: 8px 12px; color: var(--fg);
  background: var(--card); border: 1px solid var(--border); border-radius: 8px; }}
input:hover {{ border-color: var(--muted); }}
input:focus {{ outline: 2px solid var(--ring); outline-offset: 1px; border-color: transparent; }}
input[type=number] {{ font-family: var(--mono); font-size: .9rem; }}
.collect {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 0; padding: 0; border: 0; }}
.collect legend {{ float: left; margin-right: 8px; padding: 10px 0; font-size: .875rem; color: var(--muted); }}
.chip {{ display: flex; align-items: center; gap: 8px; min-height: 40px; padding: 8px 12px; font-weight: 450;
  border: 1px solid var(--border); border-radius: 8px; background: var(--card); cursor: pointer; }}
.chip:hover {{ border-color: var(--muted); }}
.chip:has(input:checked) {{ border-color: var(--fg); background: var(--soft-btn); }}
.chip input {{ width: 16px; height: 16px; min-height: 0; margin: 0; accent-color: var(--fg); cursor: pointer; }}
details {{ border-top: 1px solid var(--border); }} details:last-of-type {{ border-bottom: 1px solid var(--border); }}
summary {{ list-style: none; display: flex; align-items: center; justify-content: space-between; min-height: 48px;
  font-weight: 550; cursor: pointer; }}
summary::-webkit-details-marker {{ display: none; }}
summary::after {{ content: "+"; font: 400 1.25rem/1 var(--mono); color: var(--muted); }}
details[open] summary::after {{ content: "\\2212"; }}
.body {{ display: grid; gap: 12px; align-items: start; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); padding: 2px 0 18px; }}
.body .soft {{ font-weight: 400; }} .wide {{ grid-column: 1 / -1; }}
.body .chip {{ font-weight: 400; }}
.body textarea {{ min-height: 84px; padding: 9px 12px; color: var(--fg); background: var(--card); border: 1px solid var(--border);
  border-radius: 8px; font: .875rem/1.5 var(--mono); resize: vertical; }}
.go {{ display: flex; flex-wrap: wrap; align-items: center; gap: 10px 16px; padding-top: 4px; }}
button {{ font: inherit; min-height: 36px; padding: 7px 12px; color: var(--fg); background: var(--card);
  border: 1px solid var(--border); border-radius: 8px; cursor: pointer; }}
button:hover {{ background: var(--hover); }}
.collect-choice {{ display: flex; align-items: center; gap: 6px; }}
button.info-button {{ width: 36px; min-height: 36px; padding: 0; border-radius: 50%; font-weight: 650; color: var(--muted); }}
.info-note[popover] {{ width: min(360px, calc(100vw - 40px)); padding: 18px; border: 1px solid var(--border);
  border-radius: 10px; background: var(--card); color: var(--fg); line-height: 1.55;
  box-shadow: 0 12px 40px #0003; }}
.info-note p {{ margin: 8px 0 0; }}
button.primary {{ min-height: 42px; padding: 10px 20px; color: var(--btn-fg); background: var(--btn); border-color: var(--btn);
  font-weight: 550; letter-spacing: -.01em; }}
button.primary:hover {{ filter: brightness(1.08); background: var(--btn); }}
button:disabled {{ opacity: .55; cursor: progress; }}
:focus-visible {{ outline: 2px solid var(--ring); outline-offset: 2px; }}
#msg {{ margin: 0; min-height: 1.4em; font-size: .9rem; }} #msg.ok {{ color: var(--ok); }} #msg.bad {{ color: var(--bad); }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{ padding: 12px 8px 12px 0; text-align: left; vertical-align: top; border-bottom: 1px solid var(--border); }}
th {{ padding-top: 0; color: var(--muted); font: 550 .7rem/1.4 var(--mono); letter-spacing: .06em; text-transform: uppercase; }}
td.when {{ font-family: var(--mono); font-size: .8rem; color: var(--muted); white-space: nowrap; }}
.state {{ display: flex; align-items: center; gap: 8px; font-weight: 550; }}
.state::before {{ content: ""; flex: none; width: 7px; height: 7px; border-radius: 50%; background: var(--muted); }}
.state.ok::before {{ background: var(--ok); }} .state.failed {{ color: var(--bad); }} .state.failed::before {{ background: var(--bad); }}
.state.working::before {{ background: var(--warn); }}
.count {{ font-family: var(--mono); font-size: .8rem; font-weight: 400; color: var(--muted); }}
progress {{ display: block; width: 100%; max-width: 200px; height: 4px; margin-top: 8px; accent-color: var(--fg); }}
.note {{ margin-top: 4px; color: var(--muted); font-size: .8rem; }}
.row {{ display: flex; flex-wrap: wrap; gap: 6px; }} .scroll {{ overflow-x: auto; }} [hidden] {{ display: none !important; }}
@media (max-width: 560px) {{ button.primary {{ width: 100%; }}
  thead {{ display: none; }} tr, td {{ display: block; }} tr {{ padding: 12px 0; border-bottom: 1px solid var(--border); }}
  td {{ padding: 2px 0; border: 0; }} td.when {{ font-size: .75rem; }} td .row {{ margin-top: 8px; }} }}
@media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; }} }}
</style></head><body><main>
<header class="top"><h1>{brand}</h1>
<nav><span class="mono">v{version}</span><a href="/api/docs">API</a>
<form method="post" action="/logout" style="display:inline;margin:0">{logout}</form></nav></header>
<div class="update" id="update" role="status" hidden><b>Kit v<span id="up-v"></span> disponível</b> (esta cópia é v{version}).
  <a id="up-link" href="{repo}/releases/latest" target="_blank" rel="noopener">O que mudou</a> &middot;
  atualize com <code>bash scripts/update.sh</code>, ou <code>docker compose up -d --build</code> no Docker.</div>
<section aria-labelledby="h-new"><h2 id="h-new">Nova busca</h2>
<form id="f">
  <div class="ww">
    <div class="pick"><label for="what">O quê</label><div class="box">
      <div class="search"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></svg>
        <input id="what" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="what-opts"
          autocomplete="off" placeholder="ex.: Dentistas"></div>
      <ul class="opts" id="what-opts" role="listbox" aria-label="Tipos de negócio" hidden></ul></div>
      <div class="row tags" id="whats"></div></div>
    <div class="pick"><label for="where">Onde</label><div class="box">
      <div class="search"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 21s-7-6.2-7-11.5a7 7 0 0 1 14 0C19 14.8 12 21 12 21z"/>
        <circle cx="12" cy="9.5" r="2.5"/></svg>
        <input id="where" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="where-opts"
          autocomplete="off" placeholder="ex.: São Paulo"></div>
      <ul class="opts" id="where-opts" role="listbox" aria-label="Cidades" hidden></ul></div>
      <div class="row tags" id="wheres"></div></div>
  </div>
  <p class="hint soft">Escolha nas listas; adicione quantos quiser. Cada tipo de negócio é buscado em cada cidade, um job por cidade.</p>
  <fieldset class="collect"><legend>Coletar</legend>
    <label class="chip"><input type="checkbox" id="email" checked> E-mails</label>
    <div class="collect-choice">
      <label class="chip"><input type="checkbox" id="socials" checked> Contas sociais</label>
      <button type="button" class="info-button" popovertarget="socials-help"
        aria-label="Sobre contas sociais e velocidade"
        title="Encontra links de perfis sociais nos sites. Isso pode deixar a busca mais lenta."><span aria-hidden="true">!</span></button>
    </div>
    <div class="collect-choice">
      <label class="chip"><input type="checkbox" id="browser-sites"> Renderizar sites (Playwright)</label>
      <button type="button" class="info-button" popovertarget="browser-help"
        aria-label="Sobre renderização de sites e velocidade"
        title="Tenta de novo com um navegador. Pode deixar a busca mais lenta; use só se precisar."><span aria-hidden="true">!</span></button>
    </div>
    <div id="socials-help" class="info-note" popover role="note" aria-label="Coleta de contas sociais">
      <strong>Contas sociais</strong>
      <p>Encontra links de Instagram, Facebook, LinkedIn, TikTok, YouTube e X no site de cada empresa.</p>
      <p>Checar sites pode deixar a busca mais lenta do que coletar só as listagens do Maps. Páginas já baixadas para e-mails são reaproveitadas.</p>
      <p>Desative se não precisar de contas sociais.</p>
    </div>
    <div id="browser-help" class="info-note" popover role="note" aria-label="Renderização de sites">
      <strong>Renderizar sites</strong>
      <p>Tenta de novo com um navegador quando a checagem rápida não acha e-mails ou redes sociais.</p>
      <p>Pode deixar a busca mais lenta. As tentativas compartilham um orçamento de 30 segundos por job, mais a limpeza. Deixe desligado para a execução mais rápida.</p>
    </div>
  </fieldset>
  <details><summary>Localização e idioma</summary><div class="body">
    <p class="wide soft">Cada job centraliza o mapa na cidade escolhida.</p>
    <label for="zoom">Zoom<input id="zoom" type="number" min="1" max="21" value="15"></label>
    <label for="lang">Idioma<input id="lang" value="pt" maxlength="2" pattern="[A-Za-z]{{2}}"></label>
    <label for="country"><span>País <span class="soft">opcional</span></span><input id="country" maxlength="2" pattern="[A-Za-z]{{2}}" placeholder="da cidade"></label>
  </div></details>
  <details><summary>Opções avançadas</summary><div class="body">
    <label for="depth">Profundidade<input id="depth" type="number" min="1" max="1000" value="5">
      <span class="soft">Cerca de 20 lugares cada.</span></label>
    <label for="max_time">Limite de tempo (segundos)<input id="max_time" type="number" min="1" max="86400" value="600"></label>
    <label for="reviews">Avaliações por lugar<input id="reviews" type="number" min="0" max="5" value="0">
      <span class="soft">0 a 5; cerca de 2–4 s a mais por lugar.</span></label>
    <label class="chip wide"><input type="checkbox" id="verify"> Verificar o domínio de cada e-mail (registro MX) e descartar os que não existem</label>
  </div></details>
  <details><summary>Proxies</summary><div class="body">
    <label class="wide" for="proxies">Um por linha
      <textarea id="proxies" rows="3" autocomplete="off" placeholder="ex.: http://user:pass@proxy.example.com:8000"></textarea>
      <span class="soft">http:// ou https://, com ou sem user:pass; socks5:// sem login. As buscas se revezam,
      e se o Google recusar, passa para o próximo. Senhas aparecem como ***.</span></label>
  </div></details>
  <div class="go"><button class="primary" id="go">Iniciar scraping</button><p id="msg" role="status" aria-live="polite"></p></div>
  <label id="keybox" for="key" hidden>Chave da API (SCRAPER_API_KEY)<input id="key" type="password" autocomplete="off"></label>
</form></section>
<section aria-labelledby="h-jobs"><h2 id="h-jobs">Jobs</h2>
<div class="scroll"><table><thead><tr><th>Busca</th><th>Criado</th><th>Status</th><th>Resultados</th></tr></thead>
<tbody id="jobs"></tbody></table></div></section>
</main>
<script nonce="{nonce}">
const $ = id => document.getElementById(id);
let key = ''; try {{ key = localStorage.getItem('gmskit-key') || ''; }} catch (e) {{}}
$('key').value = key;
$('key').addEventListener('change', () => {{ key = $('key').value; try {{ localStorage.setItem('gmskit-key', key); }} catch (e) {{}} load(); }});
function api(method, path, body) {{
  const h = {{}}; if (key) h['X-API-Key'] = key; if (body) h['Content-Type'] = 'application/json';
  return fetch(path, {{method, headers: h, body: body ? JSON.stringify(body) : undefined}}).then(r => {{
    if (r.status === 401) {{ $('keybox').hidden = false; throw new Error('Este engine exige a chave da API: digite-a abaixo.'); }}
    return r;
  }});
}}
function say(t, kind) {{ $('msg').textContent = t; $('msg').className = kind || ''; }}
function cell(tr, text, cls) {{ const td = document.createElement('td'); td.textContent = text; if (cls) td.className = cls; tr.appendChild(td); return td; }}
function button(td, label, fn) {{ const b = document.createElement('button'); b.type = 'button'; b.textContent = label; b.addEventListener('click', fn); td.appendChild(b); }}
async function download(j, fmt) {{
  const r = await api('GET', '/api/v1/jobs/' + j.ID + '/download' + (fmt === 'csv' ? '' : '?format=' + fmt));
  if (!r.ok) return say('Ainda não há resultados para este job.', 'bad');
  const a = document.createElement('a'); a.href = URL.createObjectURL(await r.blob());
  a.download = fileName(j) + (fmt === 'leads' ? '-leads.csv' : '.' + fmt); document.body.appendChild(a); a.click(); a.remove();
}}
function fileName(j) {{
  const s = (j.Name || '').normalize('NFKD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase()
    .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60);
  return (s || 'results') + '-' + j.ID.slice(0, 8);
}}
const LABEL = {{pending: 'aguardando', working: 'em andamento', ok: 'concluído', failed: 'falhou'}};
function progress(j) {{
  const p = j.Progress || {{}}, name = LABEL[j.Status] || j.Status;
  if (j.Status === 'pending') return p.keywords_done ? name + ' (retoma na palavra ' + (p.keywords_done + 1) + ')' : name;
  if (j.Status !== 'working') return name + (p.places ? ' · ' + p.places + ' lugares' : '');
  const stage = {{search: 'palavra ' + Math.min(p.keywords_done + 1, p.keywords_total) + ' de ' + p.keywords_total,
    reviews: 'lendo avaliações' + (p.reviews_total ? ' ' + p.reviews_done + ' de ' + p.reviews_total : ''),
    websites: 'checando sites'}}[p.stage] || p.stage || '';
  return name + ' · ' + stage + ' · ' + (p.places || 0) + ' lugares';
}}
async function remove(j) {{
  if (!confirm('Excluir este job e os resultados?')) return;
  const r = await api('DELETE', '/api/v1/jobs/' + j.ID); say(r.ok ? 'Excluído.' : 'Não foi possível excluir.', r.ok ? 'ok' : 'bad'); load();
}}
async function load() {{
  try {{
    const list = await (await api('GET', '/api/v1/jobs')).json() || [];
    const tb = $('jobs'); tb.replaceChildren();
    for (const j of list) {{
      const tr = document.createElement('tr');
      const name = cell(tr, j.Name); if (j.Error) {{ const s = document.createElement('div'); s.className = 'note'; s.textContent = j.Error; name.appendChild(s); }}
      const when = new Date(j.Date);
      cell(tr, isNaN(when) ? j.Date : when.toLocaleString('pt-BR', {{dateStyle: 'medium', timeStyle: 'short'}}), 'when');
      const st = cell(tr, ''), s = document.createElement('div');
      const [word, ...more] = progress(j).split(' · ');
      s.className = 'state ' + j.Status; s.textContent = word; st.appendChild(s);
      if (more.length) {{ const c = document.createElement('div'); c.className = 'count'; c.textContent = more.join(' · '); st.appendChild(c); }}
      if (j.Status === 'working') {{
        const bar = document.createElement('progress'); bar.max = 100; bar.value = (j.Progress || {{}}).percent || 0;
        bar.setAttribute('aria-label', 'Progresso de ' + j.Name); st.appendChild(bar);
      }}
      const act = cell(tr, ''), row = document.createElement('div'); row.className = 'row'; act.appendChild(row);
      if (j.Status !== 'pending' || (j.Progress || {{}}).places) {{
        button(row, 'CSV leads', () => download(j, 'leads')); button(row, 'CSV completo', () => download(j, 'csv')); button(row, 'JSON', () => download(j, 'json'));
      }}
      button(row, 'Excluir', () => remove(j));
      tb.appendChild(tr);
    }}
    if (!list.length) {{ const tr = document.createElement('tr'); cell(tr, 'Nenhum job ainda. Inicie uma busca acima.', 'soft').colSpan = 4; tb.appendChild(tr); }}
  }} catch (e) {{ say(e.message, 'bad'); }}
}}
const TYPES = {types};
const whats = [], wheres = [];
function tags(box, list) {{
  box.replaceChildren();
  list.forEach((v, i) => {{
    const b = document.createElement('button'), text = v.label || v; b.type = 'button'; b.className = 'tag';
    b.textContent = text + ' ×'; b.setAttribute('aria-label', 'Remover ' + text);
    b.addEventListener('click', () => {{ list.splice(i, 1); tags(box, list); }}); box.appendChild(b);
  }});
}}
function combo(input, box, source, choose) {{
  let items = [], active = -1, stale = false;
  function show(list) {{
    items = list; active = list.length ? 0 : -1; stale = false; box.replaceChildren();
    list.forEach((it, i) => {{
      const li = document.createElement('li'); li.id = box.id + '-' + i; li.setAttribute('role', 'option'); li.textContent = it.label;
      li.addEventListener('mousedown', ev => {{ ev.preventDefault(); pick(i); }}); box.appendChild(li);
    }});
    mark(); box.hidden = !list.length; input.setAttribute('aria-expanded', String(!box.hidden));
  }}
  function mark() {{
    [...box.children].forEach((li, i) => li.setAttribute('aria-selected', String(i === active)));
    if (active < 0) return input.removeAttribute('aria-activedescendant');
    input.setAttribute('aria-activedescendant', box.id + '-' + active); box.children[active].scrollIntoView({{block: 'nearest'}});
  }}
  function pick(i) {{ choose(items[i]); input.value = ''; say('', ''); source('', show); }}
  input.addEventListener('input', () => {{
    items = []; active = -1; stale = true; mark(); if ($('msg').className === 'bad') say('', ''); source(input.value.trim(), show); }});
  input.addEventListener('focus', () => source(input.value.trim(), show));
  input.addEventListener('blur', () => show([]));
  input.addEventListener('keydown', ev => {{
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {{
      ev.preventDefault(); if (!items.length) return;
      active = (active + (ev.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length; mark();
    }} else if (ev.key === 'Enter') {{
      ev.preventDefault();
      if (active >= 0) pick(active);
      else if (input.value.trim() && !stale) say('"' + input.value.trim() + '" não está na lista. Digite algumas letras e escolha.', 'bad');
    }} else if (ev.key === 'Escape') show([]);
  }});
}}
combo($('what'), $('what-opts'), (q, show) => {{
  q = q.toLowerCase();
  const hit = TYPES.filter(t => t.toLowerCase().includes(q)), first = hit.filter(t => t.toLowerCase().startsWith(q));
  show(first.concat(hit.filter(t => !first.includes(t))).slice(0, 50).map(t => ({{label: t}})));
}}, t => {{ if (!whats.includes(t.label)) whats.push(t.label); tags($('whats'), whats); }});
(function () {{
  const key = 'gmskit-categories', month = 30 * 86400000, base = s => s.toLowerCase().replace(/s$/, '');
  function add(names) {{
    const have = new Set(TYPES.map(base));
    for (const n of names) if (!have.has(base(n))) {{ have.add(base(n)); TYPES.push(n); }}
  }}
  try {{
    const c = JSON.parse(localStorage.getItem(key) || 'null');
    if (c && Date.now() - c.t < month && Array.isArray(c.names)) return add(c.names);
  }} catch (e) {{}}
  fetch('{categories}', {{credentials: 'omit'}}).then(r => r.ok ? r.text() : '').then(t => {{
    const names = t.split('\\n').map(s => s.trim()).filter(s => s && s.length <= 80).slice(0, 5000);
    if (!names.length) return;
    add(names);
    try {{ localStorage.setItem(key, JSON.stringify({{t: Date.now(), names}})); }} catch (e) {{}}
  }}).catch(() => {{}});
}})();
let timer = 0;
combo($('where'), $('where-opts'), (q, show) => {{
  clearTimeout(timer);
  timer = setTimeout(async () => {{
    try {{
      const r = await api('GET', '/api/v1/cities?q=' + encodeURIComponent(q)); if (!r.ok) return;
      const found = await r.json();
      if (q === $('where').value.trim() && document.activeElement === $('where')) show(found);
    }} catch (e) {{ say(e.message, 'bad'); }}
  }}, 120);
}}, c => {{ if (!wheres.some(w => w.label === c.label)) wheres.push(c); tags($('wheres'), wheres); }});
$('f').addEventListener('submit', async ev => {{
  ev.preventDefault();
  for (const box of [$('what'), $('where')]) if (box.value.trim()) {{ box.focus();
    return say('"' + box.value.trim() + '" ainda não foi escolhido. Escolha na lista ou limpe o campo.', 'bad'); }}
  if (!whats.length || !wheres.length) {{ (whats.length ? $('where') : $('what')).focus();
    return say('Escolha pelo menos um tipo de negócio e uma cidade nas listas.', 'bad'); }}
  const proxies = $('proxies').value.split('\\n').map(s => s.trim()).filter(Boolean);
  const go = $('go'); go.disabled = true; go.textContent = 'Iniciando…';
  let started = 0;
  try {{
    for (const c of wheres) {{
      const keywords = whats.map(w => w + ' in ' + c.label);
      const body = {{name: Array.from(keywords[0] + (keywords.length > 1 ? ' +' + (keywords.length - 1) + ' more' : '')).slice(0, 200).join(''),
        keywords, lang: $('lang').value.trim().toLowerCase(), country: $('country').value.trim().toLowerCase() || c.country.toLowerCase(),
        lat: c.lat, lon: c.lon, zoom: +$('zoom').value || 15, depth: +$('depth').value, max_time: +$('max_time').value,
        email: $('email').checked, socials: $('socials').checked, browser_sites: $('browser-sites').checked, verify_emails: $('verify').checked,
        reviews_max: +$('reviews').value || 0}};
      if (proxies.length) body.proxies = proxies;
      const r = await api('POST', '/api/v1/jobs', body), d = await r.json();
      if (!r.ok) return say('Não iniciado' + (started ? ' após ' + started + ' job(s)' : '') + ': ' + d.message, 'bad');
      started++;
    }}
    say(started === 1 ? 'Iniciado. Roda em segundo plano e a lista abaixo atualiza sozinha.'
      : 'Iniciados ' + started + ' jobs, um por cidade. Rodam um após o outro e a lista abaixo atualiza sozinha.', 'ok');
  }} catch (e) {{ say(e.message, 'bad'); }}
  finally {{ go.disabled = false; go.textContent = 'Iniciar scraping'; load(); }}
}});
load(); setInterval(load, 3000);
(function () {{
  const key = 'gmskit-update', day = 86400000, mine = '{version}';
  const num = v => (String(v).replace(/^v/, '').split('.').slice(0, 3).map(x => parseInt(x, 10) || 0));
  const newer = (a, b) => {{ const x = num(a), y = num(b); for (let i = 0; i < 3; i++) if (x[i] !== y[i]) return x[i] > y[i]; return false; }};
  function show(tag) {{
    if (!/^v?\\d+\\.\\d+\\.\\d+/.test(tag || '') || !newer(tag, mine)) return;
    $('up-v').textContent = String(tag).replace(/^v/, '').slice(0, 20);
    $('up-link').href = '{repo}/releases/tag/' + encodeURIComponent(tag);
    $('update').hidden = false;
  }}
  try {{
    const c = JSON.parse(localStorage.getItem(key) || 'null');
    if (c && Date.now() - c.t < day) return show(c.tag);
  }} catch (e) {{}}
  fetch('{releases}', {{cache: 'no-store', credentials: 'omit', headers: {{Accept: 'application/vnd.github+json'}}}})
    .then(r => r.ok ? r.json() : null).then(d => {{
      if (!d || typeof d.tag_name !== 'string') return;
      show(d.tag_name);
      try {{ localStorage.setItem(key, JSON.stringify({{t: Date.now(), tag: d.tag_name}})); }} catch (e) {{}}
    }}).catch(() => {{}});
}})();
</script></body></html>"""

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "gmskit-engine"
    sys_version = ""
    timeout = 30  # a client that stalls mid-body or idles on keep-alive would otherwise pin a thread for good

    def log_message(self, fmt, *args):  # errors are logged by reply(); polls would flood the log
        pass

    def reply(self, code, body=b"", ctype="application/json", headers=()):
        if ctype == "application/json":
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Engine", MARK)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")  # no-referrer makes Chrome send Origin: null on form POSTs
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        if self.close_connection:  # tell the client too, so it never reuses the socket
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        if code >= 400:
            log(f"{self.command} {self.path.split('?')[0]!r} -> {code}")  # repr: no control characters in the log

    def fail(self, code, message):
        if self.command != "GET":  # a refused POST or DELETE may leave its body unread on the socket
            self.close_connection = True
        self.reply(code, {"code": code, "message": message})

    def secure_cookie(self):
        return (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower() == "https"

    def serve_login(self, error="", code=200):
        nonce = secrets.token_urlsafe(16)
        csp = (f"default-src 'none'; style-src 'nonce-{nonce}'; "
               "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        disabled = "" if self.server.ui_password else "disabled"
        if not error and not self.server.ui_password:
            error = "Acesso bloqueado: defina SCRAPER_UI_PASSWORD no .env e reinicie o engine."
        page = LOGIN.format(nonce=nonce, brand=html.escape(BRAND), error=html.escape(error),
                            disabled=disabled).encode()
        return self.reply(code, page, "text/html; charset=utf-8", [("Content-Security-Policy", csp)])

    def serve_app(self):
        nonce = secrets.token_urlsafe(16)
        csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
               "connect-src 'self' https://raw.githubusercontent.com https://api.github.com; img-src 'self' data:; "
               "form-action 'self'; base-uri 'none'; "
               "frame-ancestors 'none'")
        logout = '<button class="link" type="submit">Sair</button>'
        page = PAGE.format(nonce=nonce, brand=html.escape(BRAND), version=VERSION, repo=REPO,
                           releases=RELEASES, types=TYPES_JS, categories=CATEGORIES, logout=logout).encode()
        return self.reply(200, page, "text/html; charset=utf-8", [("Content-Security-Policy", csp)])

    def guard(self):
        """Host (DNS rebinding) and Origin (cross-site posts) checks, plus the optional API key."""
        host = self.headers.get("Host", "")
        if not host_allowed(host):
            self.close_connection = True
            self.fail(421, "this engine answers to localhost only")
            return False
        origin = self.headers.get("Origin")  # only this page: a dev server on another local port is another site
        if origin == "null" and (self.headers.get("Sec-Fetch-Site") or "").lower() == "same-origin":
            origin = None  # the browser hid the origin of a same-site form post; Sec-Fetch-Site vouches for it
        if origin is not None and not origin_allowed(origin, host):
            self.close_connection = True
            self.fail(403, "requests from other websites are refused")
            return False
        key, path = self.server.api_key, self.path.split("?")[0]
        open_paths = ("/", "/login", "/logout", "/api/docs", "/api/openapi.json")
        if key and path not in open_paths and not hmac.compare_digest(
                self.headers.get("X-API-Key", "").encode("latin-1", "replace"), key.encode()):  # headers arrive as latin-1
            self.fail(401, "missing or wrong X-API-Key")
            return False
        return True

    def body(self):
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            self.close_connection = True
            self.fail(411, "send a Content-Length")
            return _REPLIED
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= MAX_BODY:
            self.close_connection = True
            self.fail(413 if n > MAX_BODY else 400, "the body must be 1 MB or less")
            return _REPLIED
        try:
            return json.loads(self.rfile.read(n) or b"null")
        except (ValueError, RecursionError):  # bad JSON, bad UTF-8, or nested too deep to parse
            self.fail(400, "the body is not valid JSON")
            return _REPLIED

    def download(self, jid, query=""):
        data = self.server.store.read_csv(jid) if JOB_ID.match(jid or "") else None
        name = file_name(self.server.store.get(jid) if data is not None else None, jid or "")
        if data is None:
            return self.fail(404, "no results for this job (yet)")
        fmt = urllib.parse.parse_qs(query).get("format", ["csv"])[0]
        if fmt == "json":  # the same rows, every value a string, as in the CSV
            rows = list(csv.DictReader(io.StringIO(data.decode("utf-8", "replace"))))
            return self.reply(200, json.dumps(rows, ensure_ascii=False).encode(), "application/json; charset=utf-8",
                              [("Content-Disposition", f'attachment; filename="{name}.json"')])
        if fmt == "leads":
            return self.reply(200, jobs.leads_csv(data), "text/csv; charset=utf-8",
                              [("Content-Disposition", f'attachment; filename="{name}-leads.csv"')])
        if fmt != "csv":
            return self.fail(400, "format must be csv, leads or json")
        self.reply(200, jobs.guard_csv(data), "text/csv; charset=utf-8",  # a spreadsheet opens it: no formulas
                   [("Content-Disposition", f'attachment; filename="{name}.csv"')])

    def do_GET(self):
        if not self.guard():
            return
        path, _, query = self.path.partition("?")
        store = self.server.store
        if path == "/api/v1/jobs":  # null when empty, as before: the docs' health check
            return self.reply(200, [jobs.public(j) for j in store.all()] or None)
        m = JOB_PATH.match(path)
        if m:
            if m.group(2):
                return self.download(m.group(1), query)
            job = store.get(m.group(1))
            return self.reply(200, jobs.public(job)) if job else self.fail(404, "job not found")
        if path == "/download":  # the old web UI's download link
            return self.download(urllib.parse.parse_qs(query).get("id", [""])[0], query)
        if path == "/api/docs":
            nonce = secrets.token_urlsafe(16)
            csp = f"default-src 'none'; style-src 'nonce-{nonce}'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            return self.reply(200, docs_page(nonce).encode(), "text/html; charset=utf-8",
                              [("Content-Security-Policy", csp)])
        if path == "/api/v1/cities":
            return self.reply(200, find_cities(urllib.parse.parse_qs(query).get("q", [""])[0][:100]))
        if path == "/api/openapi.json":
            return self.reply(200, OPENAPI)
        if path in ("/", "/login"):
            if path == "/login" and ui_session_ok(self.server.ui_password, self.headers.get("Cookie")):
                return self.reply(303, b"", "text/plain", [("Location", "/")])
            if not ui_session_ok(self.server.ui_password, self.headers.get("Cookie")):
                return self.serve_login()
            return self.serve_app()
        self.fail(404, "not found")

    do_HEAD = do_GET  # the same headers (X-Engine included), no body

    def do_POST(self):
        if not self.guard():
            return
        path = self.path.split("?")[0]
        if path == "/login":
            pw = self.server.ui_password
            if not pw:
                return self.serve_login(
                    "Acesso bloqueado: defina SCRAPER_UI_PASSWORD no .env e reinicie o engine.", code=403)
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 <= n <= MAX_BODY:
                self.close_connection = True
                return self.fail(413 if n > MAX_BODY else 400, "the body must be 1 MB or less")
            raw = self.rfile.read(n) if n else b""
            form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
            password = (form.get("password") or [""])[0]
            # Hash both sides so compare_digest never raises on unequal lengths.
            got = hmac.new(b"gmskit-ui-login", password.encode("utf-8"), "sha256").digest()
            want = hmac.new(b"gmskit-ui-login", pw.encode("utf-8"), "sha256").digest()
            if not hmac.compare_digest(got, want):
                return self.serve_login("Senha incorreta.", code=401)
            exp = int(time.time()) + UI_TTL
            token = ui_token(pw, exp)
            return self.reply(303, b"", "text/plain", [
                ("Location", "/"),
                ("Set-Cookie", cookie_header(token, UI_TTL, self.secure_cookie())),
            ])
        if path == "/logout":
            return self.reply(303, b"", "text/plain", [
                ("Location", "/"),
                ("Set-Cookie", cookie_header("", 0, self.secure_cookie())),
            ])
        if path != "/api/v1/jobs":
            return self.fail(404, "not found")
        body = self.body()
        if body is _REPLIED:
            return
        try:
            name, data = jobs.validate(body)
        except ValueError as e:
            return self.fail(422, str(e))
        try:
            jid = self.server.store.create(name, data)
        except (UnicodeEncodeError, ValueError) as e:  # validate() keeps these out; a belt for the braces
            return self.fail(422, f"invalid body: {e}")
        except sqlite3.Error as e:  # a full disk or a locked database: answer, do not drop the connection
            return self.fail(500, f"could not store the job: {e}")
        self.server.worker.wake.set()
        log(f"job {jid} created: {len(data['keywords'])} keyword(s)")
        self.reply(201, {"id": jid})

    def do_DELETE(self):
        if not self.guard():
            return
        m = JOB_PATH.match(self.path.split("?")[0])
        if not m or m.group(2):
            return self.fail(404, "not found")
        if not self.server.store.delete(m.group(1)):  # delete first: a job claimed after the cancel sees it is gone
            return self.fail(404, "job not found")
        self.server.worker.cancel(m.group(1))
        log(f"job {m.group(1)} deleted")
        self.reply(200, {"message": "deleted"})


class Server(ThreadingHTTPServer):
    allow_reuse_address = os.name != "nt"  # on Windows SO_REUSEADDR would let a second server share the port
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind also calls socket.getfqdn(), a reverse DNS lookup that took over
        # 30 s on a macOS runner before the engine could answer. The name is never used here.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]


def port_taken(port):
    """True if something answers on 127.0.0.1:port. With SO_REUSEADDR, macOS lets a new server bind
    127.0.0.1:port while another listens on *:port (the Docker engine), and takes its traffic."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


class Server6(Server):
    address_family = socket.AF_INET6


def lock_data(folder):
    """An exclusive lock on <folder>/engine.lock, held for the engine's life: a second engine on the same
    data (serve.py --port N by hand) would flip the first one's job to pending and cut its CSV."""
    os.makedirs(folder, exist_ok=True)
    f = open(os.path.join(folder, "engine.lock"), "a+")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise OSError("its engine.lock is held")
    return f


def in_container():
    """True inside a Docker or Podman container (each leaves its marker file at the root)."""
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def make_servers(port, store, worker, api_key="", ui_password="", host="127.0.0.1"):
    """The server on host:port. On 127.0.0.1 also one on [::1]:port when this machine has IPv6
    loopback (Windows tries ::1 first for "localhost" and waits ~2 s before falling back)."""
    if port and port_taken(port):
        raise OSError(f"something already listens on port {port}")
    servers = [Server((host, port), Handler)]
    if host == "127.0.0.1":
        try:
            servers.append(Server6(("::1", servers[0].server_address[1]), Handler))
        except OSError:
            pass
    for s in servers:
        s.store, s.worker, s.api_key, s.ui_password = store, worker, api_key, ui_password
    return servers


def main():
    ap = argparse.ArgumentParser(description="The kit's Google Maps engine (local HTTP API and web UI).")
    ap.add_argument("--port", type=int, help="default: SCRAPER_PORT from the environment or .env, else 8080")
    ap.add_argument("--data", default=os.path.join(browser.HOME, "data"), help="jobs and results folder")
    ap.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "0.0.0.0"),
                    help="0.0.0.0 is for the Docker image only, where Docker publishes the port on the "
                         "host's 127.0.0.1 (default: 127.0.0.1)")
    a = ap.parse_args()
    if a.host != "127.0.0.1" and not in_container():
        # On a laptop or server 0.0.0.0 opens the engine to the network: the Host check stops DNS
        # rebinding from a browser, not a machine that sends "Host: localhost" itself.
        sys.exit("✗ --host 0.0.0.0 is for the Docker image only. On this machine the engine listens on "
                 "127.0.0.1, which keeps other machines out.")
    env = read_env()
    set_allowed_hosts(env.get("SCRAPER_ALLOWED_HOSTS", ""))
    try:
        port = a.port or int(env["SCRAPER_PORT"] or 8080)
    except ValueError:
        sys.exit(f"✗ SCRAPER_PORT is not a number: {env['SCRAPER_PORT']!r}")
    browser.make_home()
    browser.sweep_profiles()
    try:
        hold = lock_data(a.data)  # noqa: F841 - held for the engine's life
    except OSError as e:
        sys.exit(f"✗ Another engine already uses {a.data}: {e}. Stop it first (python3 scripts/engine.py stop, "
                 "or docker compose stop).")
    store = jobs.Store(a.data)
    headless = env["SCRAPER_HEADLESS"].strip().lower() not in ("0", "false", "no", "off")
    worker = jobs.Worker(store, log, headless=headless)
    try:
        servers = make_servers(port, store, worker, env["SCRAPER_API_KEY"], env["SCRAPER_UI_PASSWORD"], a.host)
    except OSError as e:
        sys.exit(f"✗ Cannot listen on {a.host}:{port}: {e}")
    # Only now: an engine that cannot bind (another one runs on this data) must not touch its jobs.
    resumed, failed = store.recover()
    if resumed:
        log(f"{resumed} job(s) were still working when the engine last stopped: they resume from their last "
            "finished keyword")
    if failed:
        log(f"{failed} job(s) stopped mid-run {jobs.MAX_RESUMES + 1} times: marked failed, rows kept")
    try:
        imported = store.import_kit1(os.path.join(browser.HOME, "kit1"), log)
    except Exception as e:  # the old jobs never stop the engine from starting
        imported = 0
        log(f"kit 1.x jobs not imported, tried again on the next start: {type(e).__name__}: {e}")
    if imported:
        log(f"{imported} job(s) from kit 1.x imported with their results")
    worker.start()
    chrome = browser.find_chrome()
    where = f"http://localhost:{port}" if a.host == "127.0.0.1" else \
        f"{a.host}:{port} in this container, published on the host's 127.0.0.1"
    extras = []
    if env["SCRAPER_API_KEY"]:
        extras.append("API key required")
    if env["SCRAPER_UI_PASSWORD"]:
        extras.append("UI login required")
    log(f"engine {VERSION} on {where} (browser: {chrome or 'none found'}{'' if headless else ', as a window'}"
        f"{', ' + ', '.join(extras) if extras else ''})")
    if not headless and browser.always_headless(chrome):
        log("SCRAPER_HEADLESS=0 is set, but the only browser is the downloaded headless shell, which cannot "
            "open a window: install Google Chrome (or Chromium, Edge, Brave) for window mode")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if hasattr(signal, "SIGBREAK"):  # Windows: scripts/engine.py stop sends Ctrl-Break to the engine's group
        signal.signal(signal.SIGBREAK, lambda *_: sys.exit(0))
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    code = 0
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as e:  # noqa: BLE001
        log(f"engine crashed: {type(e).__name__}: {e}")
        code = 1
    finally:
        worker.shutdown(wait=8)  # the job in progress saves its place and resumes on the next start
        log("engine stopped")
        # Website-pass threads may still be inside a fetch (up to SITE_HARD s each); the interpreter
        # would wait for them before closing the socket. The job is already saved: leave now.
        os._exit(code)


if __name__ == "__main__":
    main()
