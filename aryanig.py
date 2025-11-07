# aryanig_stable.py
import time
import threading
import requests
import json
import os
from flask import Flask, jsonify
from instagrapi import Client

# --- CONFIG ---
GROUPS_JSON = os.getenv("GROUPS_JSON", "[]")
CSRF_TOKEN = os.getenv("CSRF_TOKEN", "")
DOC_ID = os.getenv("DOC_ID", "29088580780787855")
BURST_COUNT = int(os.getenv("BURST_COUNT", "3"))
REFRESH_DELAY = int(os.getenv("REFRESH_DELAY", "30"))  # global cycle, used for messaging
COOLDOWN_ON_ERROR = int(os.getenv("COOLDOWN_ON_ERROR", "300"))
SELF_URL = os.getenv("SELF_URL", "")
LOGIN_STAGGER = int(os.getenv("LOGIN_STAGGER", "2"))   # seconds between starting logins to avoid bursts
MAX_LOGIN_RETRIES = int(os.getenv("MAX_LOGIN_RETRIES", "5"))

app = Flask(__name__)

# --- Observability globals ---
_state_lock = threading.Lock()
_thread_counts = {"burst": 0, "changer": 0}
_last_login = {}  # sessionid (short) -> timestamp

@app.route("/")
def home():
    return "✅ Bot running — Local test active."

@app.route("/health")
def health():
    # return basic diagnostics
    with _state_lock:
        return jsonify({
            "status": "ok",
            "message": "Bot running",
            "threads": _thread_counts,
            "last_login": _last_login
        })

def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def send_message(cl, gid, msg):
    try:
        cl.direct_send(msg, thread_ids=[int(gid)])
        log(f"✅ Sent to {gid}: {msg}")
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
            log(f"[!] {cl.username}: Error changing title -> {result['errors']}")
        else:
            log(f"[+] {cl.username}: Changed title to '{title}'")
    except Exception as e:
        log(f"[-] {cl.username}: Exception in change_title -> {e}")

# -------------------------
# Thread wrappers
# -------------------------
def safe_loop(fn, *args, restart_delay=5, **kwargs):
    """Utility: keep calling fn(*args) in a loop; if it throws, wait and restart."""
    while True:
        try:
            fn(*args, **kwargs)
            # if function returns (shouldn't), break
            log(f"[safe_loop] target {fn.__name__} returned; restarting after {restart_delay}s")
            time.sleep(restart_delay)
        except Exception as e:
            log(f"[safe_loop] Exception in {fn.__name__}: {e}. Restarting in {restart_delay}s")
            time.sleep(restart_delay)

# Messaging loop (round-robin, per account)
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
            log(f"⏳ Messenger {cl.username} for {gid}, waiting {initial_delay}s before starting...")
            time.sleep(initial_delay)
        msg_index = 0
        with _state_lock:
            _thread_counts["burst"] += 1
        while True:
            msg = messages[msg_index % len(messages)]
            for burst_num in range(BURST_COUNT):
                log(f"⏩ Round-robin burst {burst_num+1} from {cl.username} for {gid}")
                if not send_message(cl, gid, msg):
                    log(f"⚠ Error, cooling down {COOLDOWN_ON_ERROR}s")
                    time.sleep(COOLDOWN_ON_ERROR)
                else:
                    time.sleep(account_delay)
            msg_index += 1
            log(f"✅ Messenger account done cycle by {cl.username} for {gid}, waiting for global cycle ({REFRESH_DELAY * total_accounts}s)")
            time.sleep(REFRESH_DELAY * total_accounts)
    except Exception as exc:
        log(f"[burst_cycle_round_robin] Unhandled exception: {exc}")
    finally:
        with _state_lock:
            _thread_counts["burst"] = max(0, _thread_counts["burst"] - 1)

def title_changer_staggered(cl, headers, cookies, thread_id, titles, index, total_accounts):
    try:
        delay = 240
        account_delay = max(1, delay // max(1, total_accounts))
        initial_delay = account_delay * index
        if initial_delay > 0:
            log(f"⏳ TitleChanger {cl.username}, waiting {initial_delay}s to stagger start...")
            time.sleep(initial_delay)
        title_idx = 0
        with _state_lock:
            _thread_counts["changer"] += 1
        while True:
            title_to_set = titles[title_idx % len(titles)]
            log(f"📝 {cl.username} changing name for {thread_id} to '{title_to_set}'")
            change_title(cl, headers, cookies, thread_id, title_to_set)
            title_idx += 1
            time.sleep(account_delay)
    except Exception as exc:
        log(f"[title_changer_staggered] Unhandled exception: {exc}")
    finally:
        with _state_lock:
            _thread_counts["changer"] = max(0, _thread_counts["changer"] - 1)

# -------------------------
# Login helpers
# -------------------------
def login_with_backoff(session_id, max_retries=MAX_LOGIN_RETRIES):
    attempt = 0
    backoff = 2
    while True:
        attempt += 1
        try:
            cl = Client()
            cl.login_by_sessionid(session_id)
            # success
            short = session_id[-6:] if session_id else "unknown"
            with _state_lock:
                _last_login[short] = time.strftime("%Y-%m-%d %H:%M:%S")
            log(f"✅ Logged in: {cl.username} (session {short})")
            return cl
        except Exception as e:
            log(f"❌ Login attempt {attempt} failed for session ending {session_id[-6:] if session_id else 'xx'}: {e}")
            if max_retries and attempt >= max_retries:
                log(f"Reached max retries ({max_retries}) for session. Giving up for now.")
                return None
            sleep_time = backoff
            log(f"Retrying in {sleep_time}s...")
            time.sleep(sleep_time)
            backoff = min(backoff * 2, 300)  # cap backoff

# -------------------------
# Startup orchestration
# -------------------------
def start_bot_threads():
    try:
        groups_data = json.loads(GROUPS_JSON) if GROUPS_JSON else []
        log(f"[INFO] Loaded {len(groups_data)} groups.")
    except Exception as e:
        log(f"❌ GROUPS_JSON load failed: {e}")
        groups_data = []

    # Messaging (per account per group) - we stagger logins and start threads
    for group in groups_data:
        if not group.get("accounts"):
            log(f"[WARN] No accounts configured for group {group.get('thread_id')}")
            continue
        accounts = group["accounts"]
        total_accounts = len(accounts)
        for idx, acc in enumerate(accounts):
            sessionid = acc.get("session_id")
            if not sessionid:
                log(f"[WARN] Missing session_id for account idx {idx} in group {group.get('thread_id')}")
                continue

            # Do login + start burst thread in its own helper thread so main loop doesn't block
            def login_and_start_message(acc_session, group_ref, idx_ref, total_ref):
                cl = login_with_backoff(acc_session)
                if not cl:
                    log(f"[WARN] Skipping messaging thread for session ending {acc_session[-6:]} (login failed)")
                    return
                # small delay to avoid simultaneous thread start
                time.sleep(LOGIN_STAGGER * idx_ref)
                # start messaging loop inside safe_loop wrapper to auto-restart on unhandled exceptions
                threading.Thread(
                    target=lambda: safe_loop(burst_cycle_round_robin, cl, group_ref, idx_ref, total_ref, restart_delay=10),
                    daemon=True
                ).start()

            threading.Thread(target=login_and_start_message, args=(sessionid, group, idx, total_accounts), daemon=True).start()

    # Title-changers
    for group in groups_data:
        if not group.get("accounts"):
            continue
        accounts = group["accounts"]
        total_accounts = len(accounts)
        for idx, acc in enumerate(accounts):
            sessionid = acc.get("session_id")
            if not sessionid:
                continue

            def login_and_start_changer(acc_session, group_ref, idx_ref, total_ref):
                cl = login_with_backoff(acc_session)
                if not cl:
                    log(f"[WARN] Skipping changer thread for session ending {acc_session[-6:]} (login failed)")
                    return
                headers = build_headers(group_ref["thread_id"])
                cookies = build_cookies(acc_session)
                titles = acc.get("titles") if "titles" in acc else [acc.get("title", "Group")]
                time.sleep(LOGIN_STAGGER * idx_ref)
                threading.Thread(
                    target=lambda: safe_loop(title_changer_staggered, cl, headers, cookies, group_ref["thread_id"], titles, idx_ref, total_ref, restart_delay=10),
                    daemon=True
                ).start()

            threading.Thread(target=login_and_start_changer, args=(sessionid, group, idx, total_accounts), daemon=True).start()

    # Self ping if configured
    if SELF_URL:
        def self_ping_loop():
            while True:
                try:
                    requests.get(SELF_URL, timeout=10)
                    log("🔁 Self ping done (Render active).")
                except Exception as e:
                    log(f"⚠ Self ping error: {e}")
                time.sleep(60)
        threading.Thread(target=self_ping_loop, daemon=True).start()

    # keep main alive by periodically printing status to logs
    try:
        while True:
            with _state_lock:
                log(f"[STATUS] Burst threads (approx): { _thread_counts['burst'] }, Changer threads (approx): { _thread_counts['changer'] }. Logged sessions: {list(_last_login.keys())}")
            time.sleep(120)
    except KeyboardInterrupt:
        log("Exiting...")

def main():
    log("=== SCRIPT STARTED ===")
    port = int(os.getenv("PORT", "10000"))
    log(f"[INFO] Starting Flask keep-alive on port {port}")
    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True),
        daemon=True
    )
    flask_thread.start()

    # give flask a moment to bind so Render sees listening socket immediately
    time.sleep(1)

    # start bot threads after flask is up
    start_bot_threads()

if __name__ == "__main__":
    main()
