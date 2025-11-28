import os
import time
import threading
import urllib.parse
import requests
import json
from flask import Flask, jsonify
from instagrapi import Client  # normal use [web:38]

# --------- CONFIG (via env) ----------
SESSION_ID_1 = os.getenv("SESSION_ID_1")
SESSION_ID_2 = os.getenv("SESSION_ID_2")
GROUP_IDS = os.getenv("GROUP_IDS", "")  # comma separated thread ids
MESSAGE_TEXT = os.getenv("MESSAGE_TEXT", "Hello 👋")
SELF_URL = os.getenv("SELF_URL", "")

# timings (seconds)
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

@app.route("/status")
def status():
    with logs_lock:
        return jsonify({
            "ok": True,
            "sessions": {
                "acc1": {
                    "logs": session_logs["acc1"][-30:]
                },
                "acc2": {
                    "logs": session_logs["acc2"][-30:]
                },
                "system": {
                    "logs": session_logs["system"][-30:]
                }
            }
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
    """Log in using sessionid; returns Client or None"""
    session_id = decode_session(session_id)
    try:
        cl = Client()
        cl.login_by_sessionid(session_id)  # [web:38]
        uname = getattr(cl, "username", None) or name_hint or "unknown"
        log(f"✅ Logged in {uname}", session=name_hint or "system")
        return cl
    except Exception as e:
        log(f"❌ Login failed ({name_hint}): {e}", session=name_hint or "system")
        return None

def safe_send_message(cl, gid, msg, acc_name):
    """Send message and handle exceptions"""
    try:
        cl.direct_send(msg, thread_ids=[int(gid)])
        log(f"✅ {getattr(cl,'username','?')} sent to {gid}", session=acc_name)
        return True
    except Exception as e:
        log(f"⚠ Send failed ({getattr(cl,'username','?')}) -> {gid}: {e}", session=acc_name)
        return False

def safe_change_title_direct(cl, gid, new_title, acc_name):
    """Try the high-level instagrapi method first (if available)."""
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
def alternating_messages_loop(cl1, cl2, groups):
    if not groups:
        log("⚠ No groups for messaging loop.", session="system")
        return

    while True:
        try:
            for gid in groups:
                for _ in range(BURST_COUNT):
                    ok = safe_send_message(cl1, gid, MESSAGE_TEXT, "acc1")
                    if not ok:
                        log(
                            f"⚠ send failed by {getattr(cl1,'username','?')}, cooling down {COOLDOWN_ON_ERROR}s",
                            session="acc1"
                        )
                        time.sleep(COOLDOWN_ON_ERROR)
                    time.sleep(MSG_REFRESH_DELAY)
                time.sleep(0.5)
        except Exception as e:
            log(f"❌ Exception in Account1 message loop: {e}", session="acc1")

        try:
            time.sleep(DELAY_BETWEEN_MSGS)
        except Exception:
            pass

        try:
            for gid in groups:
                for _ in range(BURST_COUNT):
                    ok = safe_send_message(cl2, gid, MESSAGE_TEXT, "acc2")
                    if not ok:
                        log(
                            f"⚠ send failed by {getattr(cl2,'username','?')}, cooling down {COOLDOWN_ON_ERROR}s",
                            session="acc2"
                        )
                        time.sleep(COOLDOWN_ON_ERROR)
                    time.sleep(MSG_REFRESH_DELAY)
                time.sleep(0.5)
        except Exception as e:
            log(f"❌ Exception in Account2 message loop: {e}", session="acc2")

        try:
            time.sleep(DELAY_BETWEEN_MSGS)
        except Exception:
            pass

def alternating_title_loop(cl1, cl2, groups, titles_map):
    if not groups:
        log("⚠ No groups for title loop.", session="system")
        return

    while True:
        try:
            for gid in groups:
                titles = titles_map.get(str(gid)) or titles_map.get(int(gid)) or [MESSAGE_TEXT[:40]]
                for t in titles:
                    ok = safe_change_title_direct(cl1, gid, t, "acc1")
                    if not ok:
                        log(
                            f"⚠ Title change failed for {gid} by {getattr(cl1,'username','?')}",
                            session="acc1"
                        )
                    try:
                        time.sleep(TITLE_DELAY_BETWEEN_ACCOUNTS)
                    except Exception:
                        pass
        except Exception as e:
            log(f"❌ Exception in Account1 title loop: {e}", session="acc1")

        try:
            for gid in groups:
                titles = titles_map.get(str(gid)) or titles_map.get(int(gid)) or [MESSAGE_TEXT[:40]]
                for t in titles:
                    ok = safe_change_title_direct(cl2, gid, t, "acc2")
                    if not ok:
                        log(
                            f"⚠ Title change failed for {gid} by {getattr(cl2,'username','?')}",
                            session="acc2"
                        )
                    try:
                        time.sleep(TITLE_DELAY_BETWEEN_ACCOUNTS)
                    except Exception:
                        pass
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
        f"STARTUP: SESSION_ID_1={repr(SESSION_ID_1)}, SESSION_ID_2={repr(SESSION_ID_2)}, "
        f"GROUP_IDS={repr(GROUP_IDS)}, MESSAGE_TEXT={repr(MESSAGE_TEXT)}",
        session="system"
    )

    s1 = decode_session(SESSION_ID_1)
    s2 = decode_session(SESSION_ID_2)
    if not s1 or not s2:
        log("❌ SESSION_ID_1 and SESSION_ID_2 are required in environment", session="system")
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
        log("❌ Account 1 login failed — aborting start", session="system")
        return

    log("🔐 Logging in account 2...", session="system")
    cl2 = login_session(s2, "acc2")
    if not cl2:
        log("❌ Account 2 login failed — aborting start", session="system")
        return

    try:
        t1 = threading.Thread(target=alternating_messages_loop, args=(cl1, cl2, groups), daemon=True)
        t1.start()
        log("▶ Started alternating message thread", session="system")
    except Exception as e:
        log(f"❌ Failed to start message thread: {e}", session="system")

    try:
        t2 = threading.Thread(target=alternating_title_loop, args=(cl1, cl2, groups, titles_map), daemon=True)
        t2.start()
        log("▶ Started alternating title-change thread", session="system")
    except Exception as e:
        log(f"❌ Failed to start title thread: {e}", session="system")

    try:
        t3 = threading.Thread(target=self_ping_loop, daemon=True)
        t3.start()
    except Exception as e:
        log(f"⚠ Failed to start self-ping thread: {e}", session="system")

# -------------------------------------------------
# Always start the bot thread for Gunicorn or Flask
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
