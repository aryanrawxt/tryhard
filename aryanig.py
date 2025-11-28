import os
import time
import threading
import urllib.parse
import requests
import json
from datetime import datetime
from flask import Flask, jsonify
from instagrapi import Client
from instagrapi.exceptions import LoginRequired

# --------- SHARED STATUS (thread-safe) ----------
status_data = {
    'accounts': {
        'acc1': {'status': 'init', 'last_check': None, 'username': None},
        'acc2': {'status': 'init', 'last_check': None, 'username': None}
    },
    'errors': {
        'message_errors': 0,
        'title_errors': 0,
        'last_message_error': None,
        'last_title_error': None
    },
    'threads': {'messages': 'running', 'titles': 'running', 'ping': 'running'},
    'last_update': None
}
status_lock = threading.Lock()

log_buffer = []  # In-memory recent logs (last 100)
MAX_LOGS = 100

# --------- CONFIG (via env) ----------
SESSION_ID_1 = os.getenv("SESSION_ID_1")
SESSION_ID_2 = os.getenv("SESSION_ID_2")
GROUP_IDS = os.getenv("GROUP_IDS", "")            
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

# --------- Logging helper (thread-safe) ----------
def log(msg):
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    full_msg = f"[{timestamp}] {msg}"
    print(full_msg, flush=True)
    
    with threading.Lock():
        log_buffer.append(full_msg)
        if len(log_buffer) > MAX_LOGS:
            log_buffer.pop(0)

# --------- STATUS ENDPOINTS ----------
@app.route("/status")
def status():
    with status_lock:
        data = status_data.copy()
    return jsonify(data)

@app.route("/logs")
def logs():
    with threading.Lock():
        return jsonify({
            "logs": log_buffer[-50:],  # Last 50 logs
            "total_logs": len(log_buffer),
            "count": len(log_buffer[-50:])
        })

@app.route("/health")
def health():
    return jsonify({"status": "ok", "message": "Bot process alive"})

# --------- Session check helper ----------
def check_session_valid(cl, acc_name):
    """Check if session is still valid using lightweight method"""
    try:
        cl.get_timeline_feed()  # Standard session validity check [web:26]
        return True
    except LoginRequired:
        log(f"❌ {acc_name} SESSION EXPIRED - LoginRequired")
        return False
    except Exception as e:
        log(f"⚠ {acc_name} session check failed: {e}")
        return False

# --------- Utility helpers ----------
def decode_session(session):
    if not session:
        return session
    try:
        return urllib.parse.unquote(session)
    except Exception:
        return session

# --------- Instagram helpers with status updates ----------
def login_session(session_id, name_hint=""):
    """Log in using sessionid; returns Client or None"""
    session_id = decode_session(session_id)
    try:
        cl = Client()
        cl.login_by_sessionid(session_id)
        uname = getattr(cl, "username", None) or name_hint or "unknown"
        log(f"✅ Logged in {uname}")
        
        # Update status
        with status_lock:
            status_data['accounts'][name_hint]['status'] = 'active'
            status_data['accounts'][name_hint]['username'] = uname
            status_data['accounts'][name_hint]['last_check'] = time.time()
            status_data['last_update'] = time.time()
        
        return cl
    except Exception as e:
        log(f"❌ Login failed ({name_hint}): {e}")
        with status_lock:
            status_data['accounts'][name_hint]['status'] = 'login_failed'
        return None

def safe_send_message(cl, gid, msg):
    """Send message and handle exceptions"""
    try:
        cl.direct_send(msg, thread_ids=[int(gid)])
        log(f"✅ {getattr(cl,'username','?')} sent to {gid}")
        return True
    except Exception as e:
        error_msg = f"⚠ Send failed ({getattr(cl,'username','?')}) -> {gid}: {e}"
        log(error_msg)
        
        # Update error counters
        with status_lock:
            status_data['errors']['message_errors'] += 1
            status_data['errors']['last_message_error'] = str(e)
            status_data['last_update'] = time.time()
        return False

def safe_change_title_direct(cl, gid, new_title):
    """Try title change with error tracking"""
    try:
        tt = cl.direct_thread(int(gid))
        try:
            tt.update_title(new_title)
            log(f"📝 {getattr(cl,'username','?')} changed title (direct) for {gid} -> {new_title}")
            return True
        except Exception:
            log(f"⚠ direct .update_title() failed for {gid} — will attempt GraphQL fallback")
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
            error_msg = f"❌ GraphQL title errors for {gid}: {result['errors']}"
            log(error_msg)
            with status_lock:
                status_data['errors']['title_errors'] += 1
                status_data['errors']['last_title_error'] = str(result['errors'])
                status_data['last_update'] = time.time()
            return False
        log(f"📝 {getattr(cl,'username','?')} changed title (graphql) for {gid} -> {new_title}")
        return True
    except Exception as e:
        error_msg = f"⚠ Title change failed for {gid}: {e}"
        log(error_msg)
        with status_lock:
            status_data['errors']['title_errors'] += 1
            status_data['errors']['last_title_error'] = str(e)
            status_data['last_update'] = time.time()
        return False

# --------- Main loops (unchanged structure, added status checks) ----------
def alternating_messages_loop(cl1, cl2, groups):
    while True:
        try:
            # Periodic session check every 10 cycles
            if time.time() % 600 < 10:  # ~10min check
                if not check_session_valid(cl1, 'acc1'):
                    with status_lock:
                        status_data['accounts']['acc1']['status'] = 'expired'
                if not check_session_valid(cl2, 'acc2'):
                    with status_lock:
                        status_data['accounts']['acc2']['status'] = 'expired'
            
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
        
        time.sleep(DELAY_BETWEEN_MSGS)
        
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
        time.sleep(DELAY_BETWEEN_MSGS)

def alternating_title_loop(cl1, cl2, groups, titles_map):
    while True:
        try:
            for gid in groups:
                titles = titles_map.get(str(gid)) or titles_map.get(int(gid)) or [MESSAGE_TEXT[:40]]
                for t in titles:
                    ok = safe_change_title_direct(cl1, gid, t)
                    if not ok:
                        log(f"⚠ Title change failed for {gid} by {getattr(cl1,'username','?')}")
                    time.sleep(TITLE_DELAY_BETWEEN_ACCOUNTS)
        except Exception as e:
            log(f"❌ Exception in Account1 title loop: {e}")
        
        try:
            for gid in groups:
                titles = titles_map.get(str(gid)) or titles_map.get(int(gid)) or [MESSAGE_TEXT[:40]]
                for t in titles:
                    ok = safe_change_title_direct(cl2, gid, t)
                    if not ok:
                        log(f"⚠ Title change failed for {gid} by {getattr(cl2,'username','?')}")
                    time.sleep(TITLE_DELAY_BETWEEN_ACCOUNTS)
        except Exception as e:
            log(f"❌ Exception in Account2 title loop: {e}")

def self_ping_loop():
    while True:
        if SELF_URL:
            try:
                requests.get(SELF_URL, timeout=10)
                log("🔁 Self ping successful")
            except Exception as e:
                log(f"⚠ Self ping failed: {e}")
        time.sleep(SELF_PING_INTERVAL)

# --------- Start bot (added initial status) ----------
def start_bot():
    log(f"STARTUP: SESSION_ID_1={repr(SESSION_ID_1)}, SESSION_ID_2={repr(SESSION_ID_2)}, GROUP_IDS={repr(GROUP_IDS)}")
    
    with status_lock:
        status_data['last_update'] = time.time()
    
    s1 = decode_session(SESSION_ID_1)
    s2 = decode_session(SESSION_ID_2)
    if not s1 or not s2:
        log("❌ SESSION_ID_1 and SESSION_ID_2 are required")
        with status_lock:
            status_data['accounts']['acc1']['status'] = 'missing_session'
            status_data['accounts']['acc2']['status'] = 'missing_session'
        return
    
    groups = [g.strip() for g in GROUP_IDS.split(",") if g.strip()]
    if not groups:
        log("❌ GROUP_IDS is empty")
        return
    
    titles_map = {}
    raw_titles = os.getenv("GROUP_TITLES", "")
    if raw_titles:
        try:
            titles_map = json.loads(raw_titles)
        except Exception:
            log("⚠ GROUP_TITLES JSON invalid, using fallback")
    
    # Login accounts
    cl1 = login_session(s1, "acc1")
    if not cl1:
        return
    cl2 = login_session(s2, "acc2")
    if not cl2:
        return
    
    # Start threads
    try:
        t1 = threading.Thread(target=alternating_messages_loop, args=(cl1, cl2, groups), daemon=True)
        t1.start()
        with status_lock:
            status_data['threads']['messages'] = 'running'
        log("▶ Started message thread")
    except Exception as e:
        log(f"❌ Message thread failed: {e}")
    
    try:
        t2 = threading.Thread(target=alternating_title_loop, args=(cl1, cl2, groups, titles_map), daemon=True)
        t2.start()
        with status_lock:
            status_data['threads']['titles'] = 'running'
        log("▶ Started title thread")
    except Exception as e:
        log(f"❌ Title thread failed: {e}")
    
    try:
        t3 = threading.Thread(target=self_ping_loop, daemon=True)
        t3.start()
        with status_lock:
            status_data['threads']['ping'] = 'running'
    except Exception as e:
        log(f"⚠ Ping thread failed: {e}")

def run_bot_once():
    try:
        threading.Thread(target=start_bot, daemon=True).start()
    except Exception as e:
        log(f"❌ Bot startup failed: {e}")

run_bot_once()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    log(f"🚀 Server starting on port {port} - /status /logs available")
    app.run(host="0.0.0.0", port=port)
