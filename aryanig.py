import os
import time
import threading
import urllib.parse
import requests
import json
from flask import Flask, jsonify
from instagrapi import Client

# --------- CONFIG (via env) ----------
SESSION_ID_1 = os.getenv("SESSION_ID_1")
SESSION_ID_2 = os.getenv("SESSION_ID_2")
GROUP_IDS = os.getenv("GROUP_IDS", "")            # comma separated thread ids
MESSAGE_TEXT = os.getenv("MESSAGE_TEXT", "Hello 👋")
SELF_URL = os.getenv("SELF_URL", "")

# timings (seconds)
DELAY_BETWEEN_MSGS = int(os.getenv("DELAY_BETWEEN_MSGS", "20"))      # 20s between account turns
TITLE_DELAY_BETWEEN_ACCOUNTS = int(os.getenv("TITLE_DELAY_BETWEEN_ACCOUNTS", "120"))  # 2m between account turns
MSG_REFRESH_DELAY = int(os.getenv("MSG_REFRESH_DELAY", "1"))        # delay between burst sends inside an account
BURST_COUNT = int(os.getenv("BURST_COUNT", "1"))                    # messages per account per group per turn
SELF_PING_INTERVAL = int(os.getenv("SELF_PING_INTERVAL", "60"))
COOLDOWN_ON_ERROR = int(os.getenv("COOLDOWN_ON_ERROR", "300"))
DOC_ID = os.getenv("DOC_ID", "29088580780787855")
CSRF_TOKEN = os.getenv("CSRF_TOKEN", "")

app = Flask(__name__)

# --------- Logging helper ----------
def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

# --------- Simple health route ----------
@app.route("/health")
def health():
    return jsonify({"status": "ok", "message": "Bot process alive"})

# --------- Utility helpers ----------
def decode_session(session):
    if not session:
        return session
    try:
        return urllib.parse.unquote(session)
    except Exception:
        return session

# --------- Instagram helpers with try/except ----------
def login_session(session_id, name_hint=""):
    """Log in using sessionid; returns Client or None"""
    session_id = decode_session(session_id)
    try:
        cl = Client()
        cl.login_by_sessionid(session_id)
        uname = getattr(cl, "username", None) or name_hint or "unknown"
        log(f"✅ Logged in {uname}")
        return cl
    except Exception as e:
        log(f"❌ Login failed ({name_hint}): {e}")
        return None

def safe_send_message(cl, gid, msg):
    """Send message and handle exceptions"""
    try:
        cl.direct_send(msg, thread_ids=[int(gid)])
        log(f"✅ {getattr(cl,'username','?')} sent to {gid}")
        return True
    except Exception as e:
        log(f"⚠ Send failed ({getattr(cl,'username','?')}) -> {gid}: {e}")
        return False

def safe_change_title_direct(cl, gid, new_title):
    """Try the high-level instagrapi method first (if available)."""
    try:
        # instagrapi has helper method `.direct_thread(...).update_title(...)`
        tt = cl.direct_thread(int(gid))
        try:
            tt.update_title(new_title)
            log(f"📝 {getattr(cl,'username','?')} changed title (direct) for {gid} -> {new_title}")
            return True
        except Exception:
            log(f"⚠ direct .update_title() failed for {gid} — will attempt GraphQL fallback")
    except Exception:
        pass

    # GraphQL fallback (uses private API)
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
                    log(f"❌ GraphQL title change errors for {gid}: {result['errors']}")
                    return False
                log(f"📝 {getattr(cl,'username','?')} changed title (graphql) for {gid} -> {new_title}")
                return True
            except Exception as e:
                log(f"⚠ Title change unexpected response for {gid}: {e} (status {resp.status_code})")
                return False
        except Exception as e:
            log(f"⚠ Exception performing GraphQL title change for {gid}: {e}")
            return False
    except Exception as e:
        log(f"⚠ Unexpected fallback error for title change {gid}: {e}")
        return False

# --------- Alternating message cycle with robust error handling ----------
def alternating_messages_loop(cl1, cl2, groups):
    if not groups:
        log("⚠ No groups for messaging loop.")
        return

    while True:
        # Account 1 turn
        try:
            for gid in groups:
                for _ in range(BURST_COUNT):
                    ok = safe_send_message(cl1, gid, MESSAGE_TEXT)
                    if not ok:
                        log(f"⚠ send failed by {getattr(cl1,'username','?')}, cooling down {COOLDOWN_ON_ERROR}s")
                        time.sleep(COOLDOWN_ON_ERROR)
                    time.sleep(MSG_REFRESH_DELAY)
                time.sleep(0.5)
        except Exception as e:
            log(f"❌ Exception in Account1 message loop: {e}")

        try:
            time.sleep(DELAY_BETWEEN_MSGS)
        except Exception:
            pass

        # Account 2 turn
        try:
            for gid in groups:
                for _ in range(BURST_COUNT):
                    ok = safe_send_message(cl2, gid, MESSAGE_TEXT)
                    if not ok:
                        log(f"⚠ send failed by {getattr(cl2,'username','?')}, cooling down {COOLDOWN_ON_ERROR}s")
                        time.sleep(COOLDOWN_ON_ERROR)
                    time.sleep(MSG_REFRESH_DELAY)
                time.sleep(0.5)
        except Exception as e:
            log(f"❌ Exception in Account2 message loop: {e}")

        try:
            time.sleep(DELAY_BETWEEN_MSGS)
        except Exception:
            pass

# --------- Alternating title-change loop with robust error handling ----------
def alternating_title_loop(cl1, cl2, groups, titles_map):
    if not groups:
        log("⚠ No groups for title loop.")
        return

    while True:
        # Account 1 turn
        try:
            for gid in groups:
                titles = titles_map.get(str(gid)) or titles_map.get(int(gid)) or [MESSAGE_TEXT[:40]]
                for t in titles:
                    ok = safe_change_title_direct(cl1, gid, t)
                    if not ok:
                        log(f"⚠ Title change failed for {gid} by {getattr(cl1,'username','?')}")
                    try:
                        time.sleep(TITLE_DELAY_BETWEEN_ACCOUNTS)
                    except Exception:
                        pass
        except Exception as e:
            log(f"❌ Exception in Account1 title loop: {e}")

        # Account 2 turn
        try:
            for gid in groups:
                titles = titles_map.get(str(gid)) or titles_map.get(int(gid)) or [MESSAGE_TEXT[:40]]
                for t in titles:
                    ok = safe_change_title_direct(cl2, gid, t)
                    if not ok:
                        log(f"⚠ Title change failed for {gid} by {getattr(cl2,'username','?')}")
                    try:
                        time.sleep(TITLE_DELAY_BETWEEN_ACCOUNTS)
                    except Exception:
                        pass
        except Exception as e:
            log(f"❌ Exception in Account2 title loop: {e}")

# --------- Self-ping thread (keeps Render awake) ----------
def self_ping_loop():
    while True:
        if SELF_URL:
            try:
                requests.get(SELF_URL, timeout=10)
                log("🔁 Self ping successful")
            except Exception as e:
                log(f"⚠ Self ping failed: {e}")
        time.sleep(SELF_PING_INTERVAL)

# --------- Orchestration / starter ----------
def start_bot():
    log(f"STARTUP: SESSION_ID_1={SESSION_ID_1}, SESSION_ID_2={SESSION_ID_2}, GROUP_IDS={GROUP_IDS}")
    # decode session ids automatically
    s1 = decode_session(SESSION_ID_1)
    s2 = decode_session(SESSION_ID_2)

    if not s1 or not s2:
        log("❌ SESSION_ID_1 and SESSION_ID_2 are required in environment")
        return

    groups = [g.strip() for g in GROUP_IDS.split(",") if g.strip()]
    if not groups:
        log("❌ GROUP_IDS is empty or invalid")
        return

    titles_map = {}
    raw_titles = os.getenv("GROUP_TITLES", "")
    if raw_titles:
        try:
            titles_map = json.loads(raw_titles)
        except Exception as e:
            log(f"⚠ GROUP_TITLES JSON parse error: {e}. Using fallback titles.")

    log("🔐 Logging in account 1...")
    cl1 = login_session(s1, "acc1")
    if not cl1:
        log("❌ Account 1 login failed — aborting start")
        return
    log("🔐 Logging in account 2...")
    cl2 = login_session(s2, "acc2")
    if not cl2:
        log("❌ Account 2 login failed — aborting start")
        return

    try:
        t1 = threading.Thread(target=alternating_messages_loop, args=(cl1, cl2, groups), daemon=True)
        t1.start()
        log("▶ Started alternating message thread")
    except Exception as e:
        log(f"❌ Failed to start message thread: {e}")

    try:
        t2 = threading.Thread(target=alternating_title_loop, args=(cl1, cl2, groups, titles_map), daemon=True)
        t2.start()
        log("▶ Started alternating title-change thread")
    except Exception as e:
        log(f"❌ Failed to start title thread: {e}")

    try:
        t3 = threading.Thread(target=self_ping_loop, daemon=True)
        t3.start()
    except Exception as e:
        log(f"⚠ Failed to start self-ping thread: {e}")

if __name__ == "__main__":
    try:
        threading.Thread(target=start_bot, daemon=True).start()
    except Exception as e:
        log(f"❌ Failed to start bot: {e}")
    port = int(os.getenv("PORT", "10000"))
    log(f"HTTP server starting on port {port}")
    try:
        app.run(host="0.0.0.0", port=port)
    except Exception as e:
        log(f"❌ Flask run failed: {e}")
