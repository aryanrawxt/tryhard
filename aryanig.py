import os
import time
import threading
import urllib.parse
import requests
import json
from flask import Flask, jsonify
from instagrapi import Client
from instagrapi.exceptions import LoginRequired  # session check [web:26]

# --------- CONFIG ----------
SESSION_ID_1 = os.getenv("SESSION_ID_1")
SESSION_ID_2 = os.getenv("SESSION_ID_2")
GROUP_IDS = os.getenv("GROUP_IDS", "")
MESSAGE_TEXT = os.getenv("MESSAGE_TEXT", "Hello 👋")
SELF_URL = os.getenv("SELF_URL", "")

DELAY_BETWEEN_MSGS = int(os.getenv("DELAY_BETWEEN_MSGS", "20"))
TITLE_DELAY_BETWEEN_ACCOUNTS = int(os.getenv("TITLE_DELAY_BETWEEN_ACCOUNTS", "120"))
MSG_REFRESH_DELAY = int(os.getenv("MSG_REFRESH_DELAY", "1"))
BURST_COUNT = int(os.getenv("BURST_COUNT", "1"))
SELF_PING_INTERVAL = int(os.getenv("SELF_PING_INTERVAL", "60"))
COOLDOWN_ON_ERROR = int(os.getenv("COOLDOWN_ON_ERROR", "300"))
DOC_ID = os.getenv("DOC_ID", "29088580780787855")
CSRF_TOKEN = os.getenv("CSRF_TOKEN", "")

app = Flask(__name__)

# --------- PER-SESSION LOG STORAGE ----------
MAX_SESSION_LOGS = 100
session_logs = {
    "acc1": [],
    "acc2": [],
    "system": []
}
logs_lock = threading.Lock()

session_info = {
    "acc1": {"username": None},
    "acc2": {"username": None}
}

# --------- Logging helper ----------
def log(msg, session="system"):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    full = f"[{ts}] {msg}"
    print(full, flush=True)

    if session not in session_logs:
        session = "system"

    with logs_lock:
        session_logs[session].append(msg)
        if len(session_logs[session]) > MAX_SESSION_LOGS:
            session_logs[session].pop(0)

def set_username(acc_name, username):
    with logs_lock:
        session_info[acc_name]["username"] = username

# --------- Routes ----------
@app.route("/health")
def health():
    return jsonify({"status": "ok", "message": "Bot process alive"})

@app.route("/status")
def status():
    with logs_lock:
        return jsonify({
            "ok": True,
            "sessions": {
                "acc1": {
                    "username": session_info["acc1"]["username"] or "acc1",
                    "logs": session_logs["acc1"][-30:]
                },
                "acc2": {
                    "username": session_info["acc2"]["username"] or "acc2",
                    "logs": session_logs["acc2"][-30:]
                },
                "system": {
                    "logs": session_logs["system"][-30:]
                }
            }
        })

# --------- Helpers ----------
def decode_session(session):
    if not session:
        return session
    try:
        return urllib.parse.unquote(session)
    except Exception:
        return session

# --------- Instagram / session helpers ----------
def login_session(session_id, acc_name):
    session_id = decode_session(session_id)
    try:
        cl = Client()
        cl.login_by_sessionid(session_id)  # [web:38]
        uname = getattr(cl, "username", None) or acc_name or "unknown"
        set_username(acc_name, uname)
        log(f"✅ Logged in {uname}", session=acc_name)
        return cl
    except Exception as e:
        log(f"❌ Login failed ({acc_name}): {e}", session=acc_name)
        return None

def check_session_valid(cl, acc_name):
    try:
        cl.get_timeline_feed()  # light validity check [web:26]
        return True
    except LoginRequired:
        log(f"❌ {acc_name} session expired (LoginRequired)", session=acc_name)
        return False
    except Exception as e:
        log(f"⚠ {acc_name} session check failed: {e}", session=acc_name)
        return False

def safe_send_message(cl, gid, msg, acc_name):
    try:
        cl.direct_send(msg, thread_ids=[int(gid)])
        log(f"✅ {getattr(cl,'username','?')} sent to {gid}", session=acc_name)
        return True
    except Exception as e:
        log(f"⚠ Send failed ({getattr(cl,'username','?')}) -> {gid}: {e}", session=acc_name)
        return False

def safe_change_title_direct(cl, gid, new_title, acc_name):
    try:
        tt = cl.direct_thread(int(gid))
        try:
            tt.update_title(new_title)
            log(
                f"📝 {getattr(cl,'username','?')} changed title (direct) for {gid} -> {new_title}",
                session=acc_name
            )
            return True
        except Exception:
            log(
                f"⚠ direct .update_title() failed for {gid} — trying GraphQL",
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
        cl.private.headers.update(headers)
        cl.private.cookies.update(cookies)
        variables = {"thread_fbid": gid, "new_title": new_title}
        payload = {"doc_id": DOC_ID, "variables": json.dumps(variables)}
        resp = cl.private.post("https://www.instagram.com/api/graphql/", data=payload, timeout=10)
        result = resp.json()
        if "errors" in result:
            log(
                f"❌ GraphQL title errors for {gid}: {result['errors']}",
                session=acc_name
            )
            return False
        log(
            f"📝 {getattr(cl,'username','?')} changed title (graphql) for {gid} -> {new_title}",
            session=acc_name
        )
        return True
    except Exception as e:
        log(f"⚠ Title change failed for {gid}: {e}", session=acc_name)
        return False

# --------- Loops ----------
def alternating_messages_loop(cl1, cl2, groups):
    while True:
        try:
            check_session_valid(cl1, "acc1")
            for gid in groups:
                for _ in range(BURST_COUNT):
                    ok = safe_send_message(cl1, gid, MESSAGE_TEXT, "acc1")
                    if not ok:
                        time.sleep(COOLDOWN_ON_ERROR)
                    time.sleep(MSG_REFRESH_DELAY)
                time.sleep(0.5)
        except Exception as e:
            log(f"❌ Exception in Account1 message loop: {e}", session="acc1")

        time.sleep(DELAY_BETWEEN_MSGS)

        try:
            check_session_valid(cl2, "acc2")
            for gid in groups:
                for _ in range(BURST_COUNT):
                    ok = safe_send_message(cl2, gid, MESSAGE_TEXT, "acc2")
                    if not ok:
                        time.sleep(COOLDOWN_ON_ERROR)
                    time.sleep(MSG_REFRESH_DELAY)
                time.sleep(0.5)
        except Exception as e:
            log(f"❌ Exception in Account2 message loop: {e}", session="acc2")

        time.sleep(DELAY_BETWEEN_MSGS)

def alternating_title_loop(cl1, cl2, groups, titles_map):
    while True:
        try:
            for gid in groups:
                titles = titles_map.get(str(gid)) or titles_map.get(int(gid)) or [MESSAGE_TEXT[:40]]
                for t in titles:
                    safe_change_title_direct(cl1, gid, t, "acc1")
                    time.sleep(TITLE_DELAY_BETWEEN_ACCOUNTS)
        except Exception as e:
            log(f"❌ Exception in Account1 title loop: {e}", session="acc1")

        try:
            for gid in groups:
                titles = titles_map.get(str(gid)) or titles_map.get(int(gid)) or [MESSAGE_TEXT[:40]]
                for t in titles:
                    safe_change_title_direct(cl2, gid, t, "acc2")
                    time.sleep(TITLE_DELAY_BETWEEN_ACCOUNTS)
        except Exception as e:
            log(f"❌ Exception in Account2 title loop: {e}", session="acc2")

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
        f"STARTUP: SESSION_ID_1 set={bool(SESSION_ID_1)}, "
        f"SESSION_ID_2 set={bool(SESSION_ID_2)}, GROUP_IDS={repr(GROUP_IDS)}",
        session="system"
    )

    s1 = decode_session(SESSION_ID_1)
    s2 = decode_session(SESSION_ID_2)
    if not s1 or not s2:
        log("❌ SESSION_ID_1 and SESSION_ID_2 are required", session="system")
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

    log("🔐 Logging in account 1...", session="system")
    cl1 = login_session(s1, "acc1")
    if not cl1:
        return

    log("🔐 Logging in account 2...", session="system")
    cl2 = login_session(s2, "acc2")
    if not cl2:
        return

    try:
        threading.Thread(target=alternating_messages_loop, args=(cl1, cl2, groups), daemon=True).start()
        log("▶ Started alternating message thread", session="system")
    except Exception as e:
        log(f"❌ Failed to start message thread: {e}", session="system")

    try:
        threading.Thread(target=alternating_title_loop, args=(cl1, cl2, groups, titles_map), daemon=True).start()
        log("▶ Started alternating title-change thread", session="system")
    except Exception as e:
        log(f"❌ Failed to start title thread: {e}", session="system")

    try:
        threading.Thread(target=self_ping_loop, daemon=True).start()
        log("▶ Started self-ping thread", session="system")
    except Exception as e:
        log(f"⚠ Failed to start self-ping thread: {e}", session="system")

def run_bot_once():
    try:
        threading.Thread(target=start_bot, daemon=True).start()
    except Exception as e:
        log(f"❌ Failed to start bot (import-time): {e}", session="system")

run_bot_once()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    log(f"HTTP server starting on port {port}", session="system")
    app.run(host="0.0.0.0", port=port)
