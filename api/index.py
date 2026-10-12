"""Unstop Round Reminder - hosted backend (Vercel serverless function).

Every /api/* request is routed here by vercel.json. Data lives in Upstash Redis,
reminders go out as browser push notifications, and /api/cron (called by a
scheduler every few minutes) sends due reminders and re-syncs from Unstop.

Environment variables:
  APP_PASSWORD   password for the web app (your Unstop tokens live on this server)
  CRON_SECRET    secret the scheduler sends to /api/cron
  KV_REST_API_URL / KV_REST_API_TOKEN   (or UPSTASH_REDIS_REST_URL / _TOKEN)
                 set automatically when you connect Upstash Redis in Vercel

Run locally for testing (stores data in local_store.json):  python api/index.py
"""
import base64
import hmac
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IST = timezone(timedelta(hours=5, minutes=30))  # Unstop dates without an offset are Indian time
REFRESH_EVERY_SEC = 6 * 3600  # re-sync dates from Unstop (organisers change them)
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")
ON_VERCEL = bool(os.environ.get("VERCEL"))

DEFAULT_DATA = {
    "competitions": [],
    "settings": {
        "startOffsets": [1440, 60, 15],  # minutes before a round starts
        "endOffsets": [1440, 120],       # minutes before a round closes
    },
    "dismissed": [],  # Unstop IDs you removed, so auto-sync won't re-add them
    "lastRefresh": None,
}


# ---------- storage: Upstash Redis over REST (local JSON file when testing) ----------
KV_URL = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
KV_TOKEN = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
LOCAL_STORE = os.path.join(ROOT, "local_store.json")


class NoStorage(Exception):
    pass


def kv(*cmd):
    req = urllib.request.Request(KV_URL, data=json.dumps(cmd).encode(), headers={
        "Authorization": "Bearer " + KV_TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        res = json.load(r)
    if res.get("error"):
        raise RuntimeError("storage: " + res["error"])
    return res.get("result")


def _local():
    try:
        with open(LOCAL_STORE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get(key, default=None):
    if KV_URL:
        raw = kv("GET", "unstop:" + key)
        return json.loads(raw) if raw else default
    if ON_VERCEL:
        raise NoStorage()
    return _local().get(key, default)


def put(key, value, only_if_new=False):
    """Store a value. With only_if_new, keep whatever is already there and return it."""
    if KV_URL:
        args = ["SET", "unstop:" + key, json.dumps(value, ensure_ascii=False)]
        if only_if_new and kv(*args, "NX") is None:
            return get(key)
        if not only_if_new:
            kv(*args)
        return value
    if ON_VERCEL:
        raise NoStorage()
    store = _local()
    if only_if_new and key in store:
        return store[key]
    store[key] = value
    tmp = LOCAL_STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=1, ensure_ascii=False)
    os.replace(tmp, LOCAL_STORE)
    return value


def load():
    data = get("data") or {}
    for k, v in DEFAULT_DATA.items():
        data.setdefault(k, json.loads(json.dumps(v)))
    return data


def save(data):
    put("data", data)


def load_accounts():
    return get("accounts", [])


def public_accounts(accounts):
    return [{k: v for k, v in a.items() if k != "token"} for a in accounts]


def with_accounts(data):
    comps = [dict(c, hidden=relevance(c)) for c in data["competitions"]]
    return dict(data, competitions=comps, accounts=public_accounts(load_accounts()),
                hosted=True, push=push_info())


# ---------- Unstop ----------
def unstop_id(text):
    nums = re.findall(r"\d{5,}", text or "")
    return nums[-1] if nums else None


def fetch_unstop(comp_id):
    req = urllib.request.Request(
        f"https://unstop.com/api/public/competition/{comp_id}",
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        c = json.load(r)["data"]["competition"]

    org = (c.get("organisation") or {}).get("name") or ""
    rounds = []
    for rd in c.get("rounds") or []:
        for d in rd.get("details") or []:
            rounds.append({
                "id": f"u{d.get('id')}",
                "title": d.get("title") or f"Round {rd.get('round_order', '')}",
                "start": d.get("start_date"),
                "end": d.get("end_date"),
                "source": "unstop",
            })
    if not rounds:  # some events have no rounds, only an event date
        for d in c.get("datesToshow") or []:
            rounds.append({
                "id": f"d{d.get('id')}",
                "title": d.get("title") or "Event",
                "start": d.get("important_date", "").replace(" ", "T", 1) or None,
                "end": None,
                "source": "unstop",
            })
    return {
        "id": f"c{comp_id}",
        "unstopId": str(comp_id),
        "title": c.get("title") or "Untitled",
        "org": org,
        "url": f"https://unstop.com/{c.get('public_url') or comp_id}",
        "rounds": rounds,
    }


def fetch_many(ids):
    """{id: competition} for the IDs Unstop answered; failures are skipped."""
    def one(cid):
        try:
            return cid, fetch_unstop(cid)
        except Exception as e:  # e.g. Unstop unreachable, or jobs/internships aren't competitions
            print("fetch failed", cid, e)
            return cid, None
    with ThreadPoolExecutor(8) as pool:
        return {cid: c for cid, c in pool.map(one, ids) if c}


def merge_fresh(existing, fresh):
    """Replace Unstop-sourced rounds with fresh ones; keep rounds added by hand."""
    manual = [r for r in existing.get("rounds", []) if r.get("source") != "unstop"]
    existing.update({k: fresh[k] for k in ("title", "org", "url")})
    existing["rounds"] = fresh["rounds"] + manual


def refresh_all():
    ids = [c["unstopId"] for c in load()["competitions"] if c.get("unstopId")]
    fresh = fetch_many(ids)
    data = load()
    for c in data["competitions"]:
        if c.get("unstopId") in fresh:
            merge_fresh(c, fresh[c["unstopId"]])
    data["lastRefresh"] = datetime.now(timezone.utc).isoformat()
    save(data)
    return data


# ---------- your Unstop accounts ----------
REGISTERED_ENDPOINTS = ["/api/user/registered-opportunities", "/api/personalize/upcoming-registered"]


class TokenExpired(Exception):
    pass


def unstop_get(path, token):
    req = urllib.request.Request("https://unstop.com" + path, headers={
        "User-Agent": "Mozilla/5.0", "Accept": "application/json",
        "Authorization": "Bearer " + token, "X-Requested-With": "XMLHttpRequest",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read()
            final_url = r.geturl()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise TokenExpired()
        raise
    if "/auth/login" in final_url or body.lstrip()[:1] == b"<":
        raise TokenExpired()
    return json.loads(body)


def registration_status(obj):
    """{opportunity id: {"finished", "eliminated"}} from the registered-opportunities list."""
    out = {}
    inner = obj.get("data") if isinstance(obj, dict) else None
    items = inner.get("data") if isinstance(inner, dict) else inner
    for o in items if isinstance(items, list) else []:
        if isinstance(o, dict) and str(o.get("id", "")).isdigit() and "status" in o:
            out[str(o["id"])] = {"finished": o.get("status") == "FINISHED",
                                 "eliminated": bool(o.get("userRejectedRoundStatus"))}
    return out


def first_round_over(comp, now):
    dated = [r for r in comp.get("rounds", []) if parse(r.get("start"))]
    if not dated:
        return False
    first = min(dated, key=lambda r: parse(r["start"]))
    return (parse(first.get("end")) or parse(first["start"])) <= now


def all_rounds_over(comp, now):
    times = [parse(r.get("end")) or parse(r.get("start")) for r in comp.get("rounds", [])]
    times = [t for t in times if t]
    return bool(times) and max(times) <= now


def relevance(comp, now=None):
    """Why a competition is hidden, or None if it's active.
    Ended -> hidden. Round 1 not over -> active (everyone plays round 1).
    Round 1 over -> active only if Unstop doesn't show you as eliminated."""
    now = now or datetime.now(timezone.utc)
    if comp.get("finished") or all_rounds_over(comp, now):
        return "ended"
    if first_round_over(comp, now) and comp.get("eliminated"):
        return "not shortlisted"
    return None


def opportunity_ids(obj):
    """Pull opportunity IDs out of a response whose exact shape we don't rely on."""
    ids = set()

    def walk(o):
        if isinstance(o, dict):
            if "opportunity_id" in o and str(o["opportunity_id"]).isdigit():
                ids.add(str(o["opportunity_id"]))
            elif "public_url" in o and str(o.get("id", "")).isdigit():
                ids.add(str(o["id"]))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(obj)
    return ids


def registered_ids(token):
    """{opportunity id: status dict} for everything this account registered for."""
    found = {}
    for ep in REGISTERED_ENDPOINTS:
        page, last = 1, 1
        while page <= min(last, 20):
            res = unstop_get(f"{ep}?page={page}", token)
            for oid in opportunity_ids(res):
                found.setdefault(oid, {})
            for oid, st in registration_status(res).items():
                found[oid] = st
            inner = res.get("data") if isinstance(res, dict) else None
            if isinstance(inner, dict) and isinstance(inner.get("last_page"), int):
                last = inner["last_page"]
            page += 1
    return found


def sync_accounts():
    """Add competitions you're registered for on each connected account."""
    accounts = load_accounts()
    if not accounts:
        return
    per_account = {}
    for a in accounts:
        try:
            per_account[a["label"]] = registered_ids(a["token"])
            a.update(status="ok", lastSync=datetime.now(timezone.utc).isoformat(),
                     found=len(per_account[a["label"]]))
        except TokenExpired:
            if a.get("status") != "expired":
                notify("Unstop login expired", f"Reconnect account '{a['label']}' in Unstop Reminder.", "/?tab=accounts")
            a["status"] = "expired"
        except Exception as e:
            print("sync failed", a["label"], e)
            a["status"] = f"error: {e}"
    # re-read so an account connected/disconnected meanwhile isn't lost or revived
    by_label = {a["label"]: a for a in accounts}
    put("accounts", [by_label.get(a["label"], a) for a in load_accounts()])

    status = {}  # unstop id -> status, merged across accounts
    for label, regs in per_account.items():
        for cid, st in regs.items():
            status.setdefault(cid, dict(st, account=label))

    data = load()
    have = {c.get("unstopId") for c in data["competitions"]}
    skip = set(data["dismissed"])
    now = datetime.now(timezone.utc)
    todo = [cid for cid, st in status.items() if cid not in have and cid not in skip and not st.get("finished")]
    new = {}
    for cid, comp in fetch_many(todo).items():
        st = status[cid]
        comp.update(account=st["account"], auto=True,
                    finished=False, eliminated=st.get("eliminated", False))
        if relevance(comp, now) != "ended":
            new[cid] = comp

    data = load()
    for c in data["competitions"]:
        st = status.get(c.get("unstopId"))
        if st:
            c["finished"] = st.get("finished", False)
            c["eliminated"] = st.get("eliminated", False)
    # auto-imported competitions disappear once they've ended
    data["competitions"] = [c for c in data["competitions"]
                            if not (c.get("auto") and relevance(c, now) == "ended")]
    have = {c.get("unstopId") for c in data["competitions"]}
    data["competitions"] += [c for cid, c in new.items() if cid not in have]
    save(data)
    if new:
        names = ", ".join(c["title"] for c in new.values())
        notify(f"Added {len(new)} competition{'s' if len(new) > 1 else ''} from Unstop", names[:200], "/?tab=competitions")


# ---------- browser push notifications ----------
def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def vapid_keys():
    """The server's push key pair, created on first use and kept in storage."""
    keys = get("vapid")
    if keys:
        return keys
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    k = ec.generate_private_key(ec.SECP256R1())
    keys = {"private": b64url(k.private_numbers().private_value.to_bytes(32, "big")),
            "public": b64url(k.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))}
    return put("vapid", keys, only_if_new=True)  # two first requests at once still agree on one key


def push_info():
    try:
        return {"publicKey": vapid_keys()["public"], "devices": len(get("subs", []))}
    except ImportError:  # pywebpush/cryptography not installed (local testing)
        return None


def push_subject():
    host = os.environ.get("VERCEL_PROJECT_PRODUCTION_URL")
    return f"https://{host}" if host else "mailto:unstop-reminder@example.com"


def notify(title, body, url="/"):
    """Send a push notification to every subscribed device. Returns how many got it."""
    subs = get("subs", [])
    if not subs:
        print("no devices subscribed:", title)
        return 0
    from pywebpush import WebPushException, webpush
    key = vapid_keys()["private"]
    payload = json.dumps({"title": title, "body": body, "url": url}, ensure_ascii=False)
    keep, sent = [], 0
    for s in subs:
        try:
            webpush(s, payload, vapid_private_key=key, vapid_claims={"sub": push_subject()}, ttl=6 * 3600)
            sent += 1
            keep.append(s)
        except WebPushException as e:
            code = getattr(e.response, "status_code", None)
            print("push failed", code, e)
            if code not in (404, 410):  # 404/410 = the device unsubscribed; forget it
                keep.append(s)
    if len(keep) != len(subs):
        put("subs", keep)
    return sent


# ---------- reminders ----------
def parse(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)


def human(minutes):
    if minutes <= 0:
        return "now"
    if minutes % 1440 == 0:
        d = minutes // 1440
        return f"in {d} day{'s' if d > 1 else ''}"
    if minutes % 60 == 0:
        h = minutes // 60
        return f"in {h} hour{'s' if h > 1 else ''}"
    return f"in {minutes} min"


def due_reminders(data, sent, now):
    s = data["settings"]
    out = []
    for c in data["competitions"]:
        if c.get("muted") or relevance(c, now):
            continue
        for r in c["rounds"]:
            for kind, offsets, verb in (("start", s["startOffsets"], "starts"),
                                        ("end", s["endOffsets"], "closes")):
                base = parse(r.get(kind))
                if not base or base <= now:
                    continue
                for off in offsets:
                    key = f"{r['id']}|{kind}|{off}|{r.get(kind)}"
                    if key in sent or now < base - timedelta(minutes=off):
                        continue
                    out.append((key, off, c, r, verb, base))
    # collapse missed reminders: keep the smallest offset per round/kind
    best = {}
    for item in out:
        k = item[0].rsplit("|", 3)[0] + item[4]
        if k not in best or item[1] < best[k][1]:
            best[k] = item
    return out, list(best.values())


def run_cron():
    now = datetime.now(timezone.utc)
    data = load()
    sent = get("sent", [])
    all_due, to_show = due_reminders(data, set(sent), now)
    if all_due:  # mark first, so an overlapping run doesn't send them twice
        put("sent", (sent + [k for k, *_ in all_due])[-2000:])
    for key, off, c, r, verb, base in to_show:
        local = base.astimezone(IST).strftime("%a %d %b, %I:%M %p IST")
        left = max(0, int((base - now).total_seconds() // 60))
        notify(f"{r['title']} {verb} {human(left)}", f"{c['title']}\n{local}", c.get("url") or "/")
    synced = False
    last = parse(data.get("lastRefresh"))
    if not last or (now - last).total_seconds() > REFRESH_EVERY_SEC:
        sync_accounts()
        refresh_all()
        synced = True
    return {"ok": True, "reminders": len(to_show), "synced": synced}


# ---------- HTTP ----------
STATIC = {"/": "index.html", "/index.html": "index.html", "/sw.js": "sw.js",
          "/manifest.webmanifest": "manifest.webmanifest", "/icon-192.png": "icon-192.png",
          "/icon-512.png": "icon-512.png"}  # only used when running locally; Vercel serves these itself
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
         ".webmanifest": "application/manifest+json", ".png": "image/png"}


def same(a, b):
    return bool(b) and hmac.compare_digest(a.encode(), b.encode())


class handler(BaseHTTPRequestHandler):  # Vercel looks for a class named "handler"
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def route(self):
        url = urllib.parse.urlsplit(self.path)
        return url.path.rstrip("/").split("/api/", 1)[-1], urllib.parse.parse_qs(url.query)

    def signed_in(self):
        if not APP_PASSWORD and not ON_VERCEL:
            return True  # local testing
        return same(self.headers.get("X-App-Key", ""), APP_PASSWORD)

    def cron_allowed(self, query):
        given = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        return same(given, CRON_SECRET) or same((query.get("key") or [""])[0], CRON_SECRET)

    def guard(self, fn):
        try:
            fn()
        except NoStorage:
            self.send_json({"error": "No database connected. In Vercel, open Storage and connect Upstash Redis, then redeploy."}, 503)
        except urllib.error.HTTPError as e:
            self.send_json({"error": f"Unstop returned {e.code}. Check the link."}, 502)
        except Exception as e:
            print("error:", repr(e))
            self.send_json({"error": str(e)}, 500)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if not ON_VERCEL and path in STATIC:
            name = STATIC[path]
            with open(os.path.join(ROOT, name), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", TYPES[os.path.splitext(name)[1]])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.guard(self.get_api)

    def do_POST(self):
        self.guard(self.post_api)

    def get_api(self):
        route, query = self.route()
        if route == "cron":
            return self.cron(query)
        if not APP_PASSWORD and ON_VERCEL:
            return self.send_json({"error": "Set APP_PASSWORD in your Vercel project settings, then redeploy."}, 503)
        if not self.signed_in():
            return self.send_json({"error": "Wrong or missing password."}, 401)
        if route == "data":
            return self.send_json(with_accounts(load()))
        self.send_json({"error": "not found"}, 404)

    def cron(self, query):
        if not CRON_SECRET:
            return self.send_json({"error": "Set CRON_SECRET in your Vercel project settings."}, 503)
        if not self.cron_allowed(query):
            return self.send_json({"error": "Wrong cron secret."}, 401)
        self.send_json(run_cron())

    def post_api(self):
        route, query = self.route()
        if route == "cron":
            return self.cron(query)
        if not APP_PASSWORD and ON_VERCEL:
            return self.send_json({"error": "Set APP_PASSWORD in your Vercel project settings, then redeploy."}, 503)
        if not self.signed_in():
            return self.send_json({"error": "Wrong or missing password."}, 401)
        body = self.read_json()
        if route == "add":
            cid = unstop_id(body.get("url"))
            if not cid:
                return self.send_json({"error": "Couldn't find a competition ID in that link."}, 400)
            fresh = fetch_unstop(cid)
            data = load()
            found = next((c for c in data["competitions"] if c.get("unstopId") == cid), None)
            if found:
                merge_fresh(found, fresh)
            else:
                data["competitions"].append(fresh)
            data["dismissed"] = [d for d in data["dismissed"] if d != cid]
            save(data)
            return self.send_json(with_accounts(data))
        if route == "save":
            data = load()
            kept = {c.get("unstopId") for c in body["competitions"]}
            removed = {c.get("unstopId") for c in data["competitions"]} - kept - {None}
            data["dismissed"] = sorted(set(data["dismissed"]) | removed)
            data["competitions"] = [{k: v for k, v in c.items() if k != "hidden"} for c in body["competitions"]]
            data["settings"] = body["settings"]
            save(data)
            return self.send_json(with_accounts(data))
        if route == "refresh":
            sync_accounts()
            return self.send_json(with_accounts(refresh_all()))
        if route == "connect":
            token = (body.get("token") or "").strip().strip('"')
            label = (body.get("label") or "").strip() or "Unstop account"
            try:
                registered_ids(token)
            except TokenExpired:
                return self.send_json({"error": "That login didn't work. Log in to Unstop again, then click the bookmark."}, 400)
            accounts = [a for a in load_accounts() if a["label"] != label and a["token"] != token]
            accounts.append({"label": label, "token": token, "status": "ok"})
            put("accounts", accounts)
            sync_accounts()
            return self.send_json(with_accounts(load()))
        if route == "disconnect":
            put("accounts", [a for a in load_accounts() if a["label"] != body.get("label")])
            return self.send_json(with_accounts(load()))
        if route == "subscribe":
            sub = body.get("subscription") or {}
            if not str(sub.get("endpoint", "")).startswith("https://") or not sub.get("keys"):
                return self.send_json({"error": "Invalid push subscription."}, 400)
            subs = [s for s in get("subs", []) if s["endpoint"] != sub["endpoint"]]
            put("subs", subs + [{"endpoint": sub["endpoint"], "keys": sub["keys"]}])
            return self.send_json(with_accounts(load()))
        if route == "unsubscribe":
            put("subs", [s for s in get("subs", []) if s["endpoint"] != body.get("endpoint")])
            return self.send_json(with_accounts(load()))
        if route == "test":
            n = notify("Unstop Reminder is working", "You'll get alerts like this before your rounds.")
            if not n:
                return self.send_json({"error": "No device has notifications turned on yet."}, 400)
            return self.send_json({"ok": True, "sent": n})
        self.send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    print(f"Unstop Reminder (hosted version, local test) at http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()
