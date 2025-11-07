import time
import threading
import requests
import json
import os
import urllib.parse
from flask import Flask, jsonify
from instagrapi import Client

# ========================== CONFIGURATION ==========================
GROUPS_JSON = os.getenv("GROUPS_JSON", "[]")
CSRF_TOKEN = os.getenv("CSRF_TOKEN", "")
DOC_ID = os.getenv("DOC_ID", "29088580780787855")

BURST_COUNT = int(os.getenv("BURST_COUNT", "3"))
REFRESH_DELAY = int(os.getenv("REFRESH_DELAY", "30"))
COOLDOWN_ON_ERROR = int(os.getenv("COOLDOWN_ON_ERROR", "300"))
SELF_URL = os.getenv("SELF_URL", "")
LOGIN_STAGGER = int(os.getenv("LOGIN_STAGGER", "2"))
MAX_LOGIN_RETRIES = int(os.getenv("MAX_LOGIN_RETRIES", "5"))

app = Flask(__name__)

# ========================== STATE TRACKERS ==========================
_state_lock = threading.Lock()
_thread_counts = {"burst": 0, "changer": 0}
_last_login = {}

def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

# ========================== ROUTES ==========================
@app.route("/")
def home():
    return "✅ Bot running — Render free plan version active."

@app.route("/health")
def health():
    with _state_lock:
        return jsonify({
            "status": "ok",
            "message": "Bot running",
            "threads": _thread_counts,
            "last_login": _last_login
        })

# ========================== HELPERS ==========================
def send_message(cl, gid, msg):
    try:
        cl.direct_send(msg, thread_ids=[int(gid)])
        log(f"✅ Sent to {gid}")
        return True
    except Exception as e:
        log(f"⚠ Send fail {gid}: {e}")
        return False

def build_headers(thread_id):
    return {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/128.0.0.0 Safari/537.36"),
        "X-CSRFToken": CSRF_TOKEN,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://www.instagram.com/direct/t/{thread_id}/",
    }

def build_cookies(sessionid):
    return {"csrftoken": CSRF_TOKEN, "sessionid": sessionid}

def change_title(cl, headers, cookies, thread_id, title):
    try:
        cl.private.headers.update(headers)
        cl.private.cookies.update(cookies)
        variables = {"thread_fbid": thread_id, "new_title": title}
        payload = {"doc_id": DOC_ID, "variables": json.dumps(variables)}
        resp = cl.private.post("https://www.instagram.com/api/graphql/", data=payload)
        result = resp.json()
        if "errors" in result:
            log(f"[!] {cl.username}: Title change error -> {result['errors']}")
        else:
            log(f"[+] {cl.username}: Changed title to '{title}'")
    except Exception as e:
        log(f"[-] {getattr(cl,'username','unknown')}: Exception changing title -> {e}")

# ========================== CORE BOT FUNCTIONS ==========================
def safe_loop(fn, *args, restart_delay=5, **kwargs):
    """Runs a function forever, restarting if it crashes."""
    while True:
        try:
            fn(*args, **kwargs)
            log(f"[safe_loop] {fn.__name__} finished; restarting in {restart_delay}s")
            time.sleep(restart_delay)
        except Exception as e:
            log(f"[safe_loop] Exception in {fn.__name__}: {e}")
            time.sleep(restart_delay)

def burst_cycle_round_robin(cl, group, index, total_accounts):
    try:
        gid = group["thread_id"]
        messages = group.get("message", ["Hello 👋"])
        if isinstance(messages, str):
            messages = [messages]
        delay = group.get("delay_between_msgs", 40)
        account_delay = max(1, delay // max(1, total_accounts))
        initial_delay = account_delay * index
        if initial_delay > 0:
            log(f"⏳ Messenger {getattr(cl,'username','?')} waiting {initial_delay}s before start...")
            time.sleep(initial_delay)
        msg_index = 0
        with _state_lock:
            _thread_counts["burst"] += 1

        while True:
            msg = messages[msg_index % len(messages)]
            for burst_num in range(BURST_COUNT):
                log(f"⏩ Burst {burst_num+1} by {getattr(cl,'username','?')} for {gid}")
                if not send_message(cl, gid, msg):
                    log(f"⚠ Error, cooling down {COOLDOWN_ON_ERROR}s")
                    time.sleep(COOLDOWN_ON_ERROR)
                else:
                    time.sleep(account_delay)
            msg_index += 1
            log(f"✅ Finished group {gid}, sleeping {REFRESH_DELAY * total_accounts}s")
            time.sleep(REFRESH_DELAY * total_accounts)

    except Exception as exc:
        log(f"[burst_cycle_round_robin] Exception: {exc}")
    finally:
        with _state_lock:
            _thread_counts["burst"] = max(0, _thread_counts["burst"] - 1)

def title_changer_staggered(cl, headers, cookies, thread_id, titles, index, total_accounts):
    try:
        delay = 240
        account_delay = max(1, delay // max(1, total_accounts))
        initial_delay = account_delay * index
        if initial_delay > 0:
            log(f"⏳ TitleChanger {cl.username} waiting {initial_delay}s...")
            time.sleep(initial_delay)
        title_idx = 0
        with _state_lock:
            _thread_counts["changer"] += 1

        while True:
            title_to_set = titles[title_idx % len(titles)]
            log(f"📝 {cl.username} renaming {thread_id} → '{title_to_set}'")
            change_title(cl, headers, cookies, thread_id, title_to_set)
            title_idx += 1
            time.sleep(account_delay)

    except Exception as exc:
        log(f"[title_changer_staggered] Exception: {exc}")
    finally:
        with _state_lock:
            _thread_counts["changer"] = max(0, _thread_counts["changer"] - 1)

# ========================== LOGIN + THREAD MANAGER ==========================
def login_with_backoff(session_id, max_retries=MAX_LOGIN_RETRIES):
    attempt = 0
    backoff = 2
    while True:
        attempt += 1
        try:
            cl = Client()
            cl.login_by_sessionid(session_id)
            short = session_id[-6:] if session_id else "unknown"
            with _state_lock:
                _last_login[short] = time.strftime("%Y-%m-%d %H:%M:%S")
            log(f"✅ Logged in: {cl.username} ({short})")
            return cl
        except Exception as e:
            log(f"❌ Login {attempt} failed ({session_id[-6:]}) → {e}")
            if max_retries and attempt >= max_retries:
                log(f"Max retries ({max_retries}) hit; skipping.")
                return None
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)

def start_bot_threads():
    try:
        groups_data = json.loads(GROUPS_JSON) if GROUPS_JSON else []
        log(f"[INFO] Loaded {len(groups_data)} groups from env.")
    except Exception as e:
        log(f"❌ GROUPS_JSON load failed: {e}")
        return

    for g in groups_data:
        for acc in g.get("accounts", []):
            sid = acc.get("session_id", "")
            if sid:
                acc["session_id"] = urllib.parse.unquote(sid)

    # --- Start message threads ---
    for group in groups_data:
        accounts = group.get("accounts", [])
        total = len(accounts)
        for idx, acc in enumerate(accounts):
            sessionid = acc.get("session_id")
            if not sessionid:
                continue

            def msg_runner(acc_session, grp, idx_ref, total_ref):
                cl = login_with_backoff(acc_session)
                if not cl:
                    return
                time.sleep(LOGIN_STAGGER * idx_ref)
                threading.Thread(
                    target=lambda: safe_loop(
                        burst_cycle_round_robin, cl, grp, idx_ref, total_ref, restart_delay=10
                    ),
                    daemon=True
                ).start()

            threading.Thread(target=msg_runner, args=(sessionid, group, idx, total), daemon=True).start()

    # --- Start title changer threads ---
    for group in groups_data:
        accounts = group.get("accounts", [])
        total = len(accounts)
        for idx, acc in enumerate(accounts):
            sessionid = acc.get("session_id")
            if not sessionid:
                continue

            def title_runner(acc_session, grp, idx_ref, total_ref):
                cl = login_with_backoff(acc_session)
                if not cl:
                    return
                headers = build_headers(grp["thread_id"])
                cookies = build_cookies(acc_session)
                titles = acc.get("titles", [acc.get("title", "Group")])
                time.sleep(LOGIN_STAGGER * idx_ref)
                threading.Thread(
                    target=lambda: safe_loop(
                        title_changer_staggered, cl, headers, cookies,
                        grp["thread_id"], titles, idx_ref, total_ref, restart_delay=10
                    ),
                    daemon=True
                ).start()

            threading.Thread(target=title_runner, args=(sessionid, group, idx, total), daemon=True).start()

    # --- Self ping keepalive (optional) ---
    if SELF_URL:
        def self_ping():
            while True:
                try:
                    requests.get(SELF_URL, timeout=10)
                    log("🔁 Self ping done (Render active).")
                except Exception as e:
                    log(f"⚠ Self ping error: {e}")
                time.sleep(60)
        threading.Thread(target=self_ping, daemon=True).start()

    # --- Main heartbeat ---
    try:
        while True:
            with _state_lock:
                log(f"[STATUS] Burst={_thread_counts['burst']} | Changer={_thread_counts['changer']} | Logins={list(_last_login.keys())}")
            time.sleep(120)
    except KeyboardInterrupt:
        log("Exiting...")

# ========================== SAFE BACKGROUND START ==========================
def _start_runner_in_daemon():
    t = threading.Thread(target=start_bot_threads, daemon=True)
    t.start()
    return t

_background_thread = _start_runner_in_daemon()

# ========================== LOCAL DEBUG ==========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    log(f"[LOCAL] Starting Flask dev server on port {port}")
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)
