import os
import time
import threading
import urllib.parse
import requests
import json
from flask import Flask, jsonify
from instagrapi import Client  # [web:16]

# --------- CONFIG (via env) ----------
SESSION_ID_1 = os.getenv("SESSION_ID_1")
SESSION_ID_2 = os.getenv("SESSION_ID_2")
SESSION_ID_3 = os.getenv("SESSION_ID_3")
SESSION_ID_4 = os.getenv("SESSION_ID_4")

GROUP_IDS = os.getenv("GROUP_IDS", "")  # comma separated thread ids
MESSAGE_TEXT = os.getenv("MESSAGE_TEXT", "Hello 👋")
SELF_URL = os.getenv("SELF_URL", "")

# NC titles: comma-separated, e.g. "hi,hello,hyy,k"
NC_TITLES_RAW = os.getenv("NC_TITLES", "")  # optional

# --------- TIMING CONFIG ----------
# Spam:
#   t≈1s:   acc1 spam
#   t≈11s:  acc2 spam
#   t≈21s:  acc3 spam
#   t≈31s:  acc4 spam
#   t≈41s:  acc1 spam again...
SPAM_START_OFFSET = 1
SPAM_GAP_BETWEEN_ACCOUNTS = 10  # seconds

# NC (title change) – 3 minute full cycle:
# 4 accounts → 180s / 4 = 45s gap between accounts
NC_START_OFFSET = 1
NC_ACC_GAP = 45  # seconds

MSG_REFRESH_DELAY = int(os.getenv("MSG_REFRESH_DELAY", "1"))
BURST_COUNT = int(os.getenv("BURST_COUNT", "1"))
SELF_PING_INTERVAL = int(os.getenv("SELF_PING_INTERVAL", "60"))
COOLDOWN_ON_ERROR = int(os.getenv("COOLDOWN_ON_ERROR", "300"))
DOC_ID = os.getenv("DOC_ID", "29088580780787855")
CSRF_TOKEN = os.getenv("CSRF_TOKEN", "")

app = Flask(__name__)

# --------- PER-SESSION LOG STORAGE ----------
MAX_SESSION_LOGS = 200
session_logs = {
    "acc1": [],
    "acc2": [],
    "acc3": [],
    "acc4": [],
    "system": []
}
logs_lock = threading.Lock()

def _push_log(session, msg):
    if session not in session_logs:
        session = "system"
    with logs_lock:
        session_logs[session].append(msg)
        if len(session_logs[session]) > MAX_SESSION_LOGS:
            session_logs[session].pop(0)

# --------- Logging helper ----------
def log(msg, session="system"):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    _push_log(session, msg)

# --------- Routes ----------
@app.route("/health")
def health():
    return jsonify({"status": "ok", "message": "Bot process alive"})

def summarize(lines):
    rev = list(reversed(lines))
    last_login = next((l for l in rev if "Logged in" in l), None)
    last_send_ok = next((l for l in rev if "✅" in l and "sent to" in l), None)
    last_send_err = next((l for l in rev if "Send failed" in l or "⚠ send failed" in l), None)
    last_title_ok = next((l for l in rev if "changed title" in l and "📝" in l), None)
    last_title_err = next((l for l in rev if "Title change" in l or "GraphQL title" in l), None)
    return {
        "last_login": last_login,
        "last_send_ok": last_send_ok,
        "last_send_error": last_send_err,
        "last_title_ok": last_title_ok,
        "last_title_error": last_title_err,
    }

@app.route("/status")
def status():
    with logs_lock:
        acc1_logs = session_logs["acc1"][-80:]
        acc2_logs = session_logs["acc2"][-80:]
        acc3_logs = session_logs["acc3"][-80:]
        acc4_logs = session_logs["acc4"][-80:]
        system_last = session_logs["system"][-5:]

    return jsonify({
        "ok": True,
        "acc1": summarize(acc1_logs),
        "acc2": summarize(acc2_logs),
        "acc3": summarize(acc3_logs),
        "acc4": summarize(acc4_logs),
        "system_last": system_last
    })

# --------- Utility helpers ----------
def decode_session(session):
    if not session:
        return session
    try:
        return urllib.parse.unquote(session)
    except Exception:
        return session

# --------- Instagram helpers ----------
def login_session(session_id, name_hint=""):
    session_id = decode_session(session_id)
    try:
        cl = Client()
        cl.login_by_sessionid(session_id)  # [web:16]
        uname = getattr(cl, "username", None) or name_hint or "unknown"
        log(f"✅ Logged in {uname}", session=name_hint or "system")
        return cl
    except Exception as e:
        log(f"❌ Login failed ({name_hint}): {e}", session=name_hint or "system")
        return None

def safe_send_message(cl, gid, msg, acc_name):
    try:
        cl.direct_send(msg, thread_ids=[int(gid)])  # [web:16]
        log(f"✅ {getattr(cl,'username','?')} sent to {gid}", session=acc_name)
        return True
    except Exception as e:
        log(f"⚠ Send failed ({getattr(cl,'username','?')}) -> {gid}: {e}", session=acc_name)
        return False

def safe_change_title_direct(cl, gid, new_title, acc_name):
    try:
        tt = cl.direct_thread(int(gid))  # [web:16]
        try:
            tt.update_title(new_title)
            log(
                f"📝 {getattr(cl,'username','?')} changed title (direct) for {gid} -> {new_title}",
                session=acc_name
            )
            return True
        except Exception:
            log(
                f"⚠ direct .update_title() failed for {gid} — will attempt GraphQL fallback",
                session=acc_name
            )
    except Exception:
        pass

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "X-CSRFToken": CSRF_TOKEN,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://www.instagram.com/direct/t/{gid}/",
        }
        cookies = {"csrftoken": CSRF_TOKEN}
        try:
            cl.private.headers.update(headers)
            cl.private.cookies.update(cookies)
            variables = {"thread_fbid": gid, "new_title": new_title}
            payload = {"doc_id": DOC_ID, "variables": json.dumps(variables)}
            resp = cl.private.post("https://www.instagram.com/api/graphql/", data=payload, timeout=10)
            try:
                result = resp.json()
                if "errors" in result:
                    log(
                        f"❌ GraphQL title change errors for {gid}: {result['errors']}",
                        session=acc_name
                    )
                    return False
                log(
                    f"📝 {getattr(cl,'username','?')} changed title (graphql) for {gid} -> {new_title}",
                    session=acc_name
                )
                return True
            except Exception as e:
                log(
                    f"⚠ Title change unexpected response for {gid}: {e} (status {resp.status_code})",
                    session=acc_name
                )
                return False
        except Exception as e:
            log(f"⚠ Exception performing GraphQL title change for {gid}: {e}", session=acc_name)
            return False
    except Exception as e:
        log(f"⚠ Unexpected fallback error for title change {gid}: {e}", session=acc_name)
        return False

# --------- Loops ----------
def spam_loop(clients, groups):
    if not groups:
        log("⚠ No groups for messaging loop.", session="system")
        return

    time.sleep(SPAM_START_OFFSET)

    idx = 0
    n = len(clients)
    while True:
        cl = clients[idx]
        acc_name = f"acc{idx+1}"

        try:
            for gid in groups:
                for _ in range(BURST_COUNT):
                    ok = safe_send_message(cl, gid, MESSAGE_TEXT, acc_name)
                    if not ok:
                        log(
                            f"⚠ send failed by {getattr(cl,'username','?')}, cooling down {COOLDOWN_ON_ERROR}s",
                            session=acc_name
                        )
                        time.sleep(COOLDOWN_ON_ERROR)
                    time.sleep(MSG_REFRESH_DELAY)
                time.sleep(0.5)
        except Exception as e:
            log(f"❌ Exception in {acc_name} message loop: {e}", session=acc_name)

        try:
            time.sleep(SPAM_GAP_BETWEEN_ACCOUNTS)
        except Exception:
            pass

        idx = (idx + 1) % n

def parse_nc_titles():
    """
    Returns a list of 4 titles, one per account.
    If NC_TITLES_RAW has fewer than 4, it pads with MESSAGE_TEXT[:40].
    """
    base = [t.strip() for t in NC_TITLES_RAW.split(",") if t.strip()]
    default_title = MESSAGE_TEXT[:40] or "NC"
    while len(base) < 4:
        base.append(default_title)
    return base[:4]

def nc_loop(clients, groups, titles_map):
    if not groups:
        log("⚠ No groups for title loop.", session="system")
        return

    # one fixed NC title per account from env
    per_account_titles = parse_nc_titles()
    log(f"NC titles per account: {per_account_titles}", session="system")

    time.sleep(NC_START_OFFSET)

    idx = 0
    n = len(clients)

    while True:
        cl = clients[idx]
        acc_name = f"acc{idx+1}"
        account_title = per_account_titles[idx]

        try:
            for gid in groups:
                # group-specific titles_map has priority; if not set, use account_title
                titles = titles_map.get(str(gid)) or titles_map.get(int(gid)) or [account_title]
                t = titles[0]
                ok = safe_change_title_direct(cl, gid, t, acc_name)
                if not ok:
                    log(
                        f"⚠ Title change failed for {gid} by {getattr(cl,'username','?')}",
                        session=acc_name
                    )
                time.sleep(1)
        except Exception as e:
            log(f"❌ Exception in {acc_name} title loop: {e}", session=acc_name)

        try:
            time.sleep(NC_ACC_GAP)
        except Exception:
            pass

        idx = (idx + 1) % n

def self_ping_loop():
    while True:
        if SELF_URL:
            try:
                requests.get(SELF_URL, timeout=10)
                log("🔁 Self ping successful", session="system")
            except Exception as e:
                log(f"⚠ Self ping failed: {e}", session="system")
        time.sleep(SELF_PING_INTERVAL)

# --------- Start bot ----------
def start_bot():
    log(
        "STARTUP: "
        f"SESSION_ID_1={repr(SESSION_ID_1)}, "
        f"SESSION_ID_2={repr(SESSION_ID_2)}, "
        f"SESSION_ID_3={repr(SESSION_ID_3)}, "
        f"SESSION_ID_4={repr(SESSION_ID_4)}, "
        f"GROUP_IDS={repr(GROUP_IDS)}, MESSAGE_TEXT={repr(MESSAGE_TEXT)}, "
        f"NC_TITLES={repr(NC_TITLES_RAW)}",
        session="system"
    )

    s1 = decode_session(SESSION_ID_1)
    s2 = decode_session(SESSION_ID_2)
    s3 = decode_session(SESSION_ID_3)
    s4 = decode_session(SESSION_ID_4)

    sessions = [s1, s2, s3, s4]
    if not all(sessions):
        log("❌ All 4 session IDs (SESSION_ID_1..4) are required in environment", session="system")
        return

    groups = [g.strip() for g in GROUP_IDS.split(",") if g.strip()]
    if not groups:
        log("❌ GROUP_IDS is empty or invalid", session="system")
        return

    titles_map = {}
    raw_titles = os.getenv("GROUP_TITLES", "")
    if raw_titles:
        try:
            titles_map = json.loads(raw_titles)
        except Exception as e:
            log(f"⚠ GROUP_TITLES JSON parse error: {e}. Using fallback titles.", session="system")

    clients = []
    for i, s in enumerate(sessions, 1):
        log(f"🔐 Logging in account {i}...", session="system")
        cl = login_session(s, f"acc{i}")
        if not cl:
            log(f"❌ Account {i} login failed — aborting start", session="system")
            return
        clients.append(cl)

    try:
        t1 = threading.Thread(target=spam_loop, args=(clients, groups), daemon=True)
        t1.start()
        log("▶ Started spam loop with 4 accounts (1s start, 10s gap between accounts)", session="system")
    except Exception as e:
        log(f"❌ Failed to start spam loop thread: {e}", session="system")

    try:
        t2 = threading.Thread(target=nc_loop, args=(clients, groups, titles_map), daemon=True)
        t2.start()
        log("▶ Started nc loop with 4 accounts (1s start, 45s gap between accounts, 3-min full cycle)", session="system")
    except Exception as e:
        log(f"❌ Failed to start nc loop thread: {e}", session="system")

    try:
        t3 = threading.Thread(target=self_ping_loop, daemon=True)
        t3.start()
    except Exception as e:
        log(f"⚠ Failed to start self-ping thread: {e}", session="system")

# -------------------------------------------------
def run_bot_once():
    try:
        threading.Thread(target=start_bot, daemon=True).start()
    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ❌ Failed to start bot (import-time): {e}", flush=True)

run_bot_once()
# -------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    log(f"HTTP server starting on port {port}", session="system")
    try:
        app.run(host="0.0.0.0", port=port)
    except Exception as e:
        log(f"❌ Flask run failed: {e}", session="system")
