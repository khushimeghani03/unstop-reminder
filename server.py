"""Unstop Round Reminder - local server.

Serves the web UI, fetches competition rounds from Unstop's public API,
stores your data in data.json and pops Windows notifications before rounds.
Standard library only. Run:  python server.py
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", 8765
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "data.json")
# Login tokens live in their own file and are never sent to the browser.
ACCOUNTS_FILE = os.path.join(HERE, "accounts.json")
DEBUG_FILE = os.path.join(HERE, "last_sync_debug.json")
CHECK_EVERY_SEC = 30
REFRESH_EVERY_SEC = 6 * 3600  # re-sync dates from Unstop (organisers change them)

DEFAULT_DATA = {
    "competitions": [],
    "settings": {
        "startOffsets": [1440, 60, 15],  # minutes before a round starts
        "endOffsets": [1440, 120],       # minutes before a round closes
    },
    "sent": [],
    "dismissed": [],  # Unstop IDs you removed, so auto-sync won't re-add them
    "lastRefresh": None,
}

lock = threading.Lock()


# ---------- storage ----------
def load():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    for k, v in DEFAULT_DATA.items():
        data.setdefault(k, json.loads(json.dumps(v)))
    return data


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def save(data):
    write_json(DATA_FILE, data)


def load_accounts():
    try:
        with open(ACCOUNTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def public_accounts(accounts):
    return [{k: v for k, v in a.items() if k != "token"} for a in accounts]


def with_accounts(data):
    comps = [dict(c, hidden=relevance(c)) for c in data["competitions"]]
    return dict(data, competitions=comps, accounts=public_accounts(load_accounts()))


# ---------- Unstop ----------
def unstop_id(text):
    nums = re.findall(r"\d{5,}", text or "")
    return nums[-1] if nums else None


def fetch_unstop(comp_id):
    req = urllib.request.Request(
        f"https://unstop.com/api/public/competition/{comp_id}",
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
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


def merge_fresh(existing, fresh):
    """Replace Unstop-sourced rounds with fresh ones; keep rounds added by hand."""
    manual = [r for r in existing.get("rounds", []) if r.get("source") != "unstop"]
    existing.update({k: fresh[k] for k in ("title", "org", "url")})
    existing["rounds"] = fresh["rounds"] + manual


def refresh_all():
    with lock:
        data = load()
        ids = [c["unstopId"] for c in data["competitions"] if c.get("unstopId")]
    fresh = {}
    for cid in ids:
        try:
            fresh[cid] = fetch_unstop(cid)
        except Exception as e:  # keep old data if Unstop is unreachable
            print("refresh failed", cid, e)
    with lock:
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
        with urllib.request.urlopen(req, timeout=20) as r:
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
    found, debug = {}, {}
    for ep in REGISTERED_ENDPOINTS:
        page, last = 1, 1
        while page <= min(last, 20):
            res = unstop_get(f"{ep}?page={page}", token)
            debug[f"{ep}?page={page}"] = res
            for oid in opportunity_ids(res):
                found.setdefault(oid, {})
            for oid, st in registration_status(res).items():
                found[oid] = st
            inner = res.get("data") if isinstance(res, dict) else None
            if isinstance(inner, dict) and isinstance(inner.get("last_page"), int):
                last = inner["last_page"]
            page += 1
    write_json(DEBUG_FILE, debug)  # local only; helps adapt if Unstop changes its format
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
                toast("Unstop login expired", f"Reconnect account '{a['label']}' in Unstop Reminder.")
            a["status"] = "expired"
        except Exception as e:
            print("sync failed", a["label"], e)
            a["status"] = f"error: {e}"
    write_json(ACCOUNTS_FILE, accounts)

    status = {}  # unstop id -> status, merged across accounts
    for label, regs in per_account.items():
        for cid, st in regs.items():
            status.setdefault(cid, dict(st, account=label))

    with lock:
        data = load()
        have = {c.get("unstopId") for c in data["competitions"]}
        skip = set(data["dismissed"])
    now = datetime.now(timezone.utc)
    new = {}
    for cid, st in status.items():
        if cid in have or cid in skip or st.get("finished"):
            continue
        try:
            comp = fetch_unstop(cid)
        except Exception as e:
            print("skip", cid, e)  # e.g. jobs/internships aren't competitions
            continue
        comp.update(account=st["account"], auto=True,
                    finished=False, eliminated=st.get("eliminated", False))
        if relevance(comp, now) != "ended":
            new[cid] = comp

    with lock:
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
        toast(f"Added {len(new)} competition{'s' if len(new) > 1 else ''} from Unstop", names[:200])


# ---------- notifications ----------
TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$x = $t.GetElementsByTagName('text')
$x.Item(0).AppendChild($t.CreateTextNode($env:TOAST_TITLE)) | Out-Null
$x.Item(1).AppendChild($t.CreateTextNode($env:TOAST_BODY)) | Out-Null
$n = [Windows.UI.Notifications.ToastNotification]::new($t)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe').Show($n)
"""


def toast(title, body):
    env = dict(os.environ, TOAST_TITLE=title, TOAST_BODY=body)
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", TOAST_PS],
        env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=30,
    )


def parse(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.astimezone()


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


def due_reminders(data, now):
    s = data["settings"]
    sent = set(data["sent"])
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
                    # only the closest pending offset fires if several were missed
                    out.append((key, off, c, r, verb, base))
    # collapse missed reminders: keep the smallest offset per round/kind
    best = {}
    for item in out:
        k = item[0].rsplit("|", 3)[0] + item[4]
        if k not in best or item[1] < best[k][1]:
            best[k] = item
    return out, list(best.values())


def reminder_loop():
    while True:
        try:
            with lock:
                data = load()
                now = datetime.now(timezone.utc)
                all_due, to_show = due_reminders(data, now)
                if all_due:
                    data["sent"].extend(k for k, *_ in all_due)
                    data["sent"] = data["sent"][-2000:]
                    save(data)
            for key, off, c, r, verb, base in to_show:
                local = base.astimezone().strftime("%a %d %b, %I:%M %p")
                left = max(0, int((base - now).total_seconds() // 60))
                toast(f"{r['title']} {verb} {human(left)}",
                      f"{c['title']}\n{local}")
            last = parse(data.get("lastRefresh"))
            if not last or (now - last).total_seconds() > REFRESH_EVERY_SEC:
                sync_accounts()
                refresh_all()
        except Exception as e:
            print("reminder loop error:", e)
        time.sleep(CHECK_EVERY_SEC)


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        self.path = self.path.split("?", 1)[0]
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/data":
            with lock:
                self.send_json(with_accounts(load()))
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            body = self.read_json()
            if self.path == "/api/add":
                cid = unstop_id(body.get("url"))
                if not cid:
                    return self.send_json({"error": "Couldn't find a competition ID in that link."}, 400)
                fresh = fetch_unstop(cid)
                with lock:
                    data = load()
                    found = next((c for c in data["competitions"] if c.get("unstopId") == cid), None)
                    if found:
                        merge_fresh(found, fresh)
                    else:
                        data["competitions"].append(fresh)
                    data["dismissed"] = [d for d in data["dismissed"] if d != cid]
                    save(data)
                    return self.send_json(with_accounts(data))
            if self.path == "/api/save":
                with lock:
                    data = load()
                    kept = {c.get("unstopId") for c in body["competitions"]}
                    removed = {c.get("unstopId") for c in data["competitions"]} - kept - {None}
                    data["dismissed"] = sorted(set(data["dismissed"]) | removed)
                    data["competitions"] = body["competitions"]
                    data["settings"] = body["settings"]
                    save(data)
                    return self.send_json(with_accounts(data))
            if self.path == "/api/refresh":
                sync_accounts()
                return self.send_json(with_accounts(refresh_all()))
            if self.path == "/api/connect":
                token = (body.get("token") or "").strip().strip('"')
                label = (body.get("label") or "").strip() or "Unstop account"
                try:
                    registered_ids(token)
                except TokenExpired:
                    return self.send_json({"error": "That login didn't work. Log in to Unstop again, then click the bookmark."}, 400)
                accounts = [a for a in load_accounts() if a["label"] != label and a["token"] != token]
                accounts.append({"label": label, "token": token, "status": "ok"})
                write_json(ACCOUNTS_FILE, accounts)
                sync_accounts()
                with lock:
                    return self.send_json(with_accounts(load()))
            if self.path == "/api/disconnect":
                write_json(ACCOUNTS_FILE, [a for a in load_accounts() if a["label"] != body.get("label")])
                with lock:
                    return self.send_json(with_accounts(load()))
            if self.path == "/api/test":
                toast("Unstop Reminder is working", "You'll get alerts like this before your rounds.")
                return self.send_json({"ok": True})
            self.send_json({"error": "not found"}, 404)
        except urllib.error.HTTPError as e:
            self.send_json({"error": f"Unstop returned {e.code}. Check the link."}, 502)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)


if __name__ == "__main__":
    threading.Thread(target=reminder_loop, daemon=True).start()
    print(f"Unstop Reminder running at http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
