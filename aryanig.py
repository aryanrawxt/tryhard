import os
import time
import threading
import requests
import json
from flask import Flask
from instagrapi import Client

app = Flask(__name__)

# --- CONFIG ---
SESSION_ID_1 = os.getenv("SESSION_ID_1")
SESSION_ID_2 = os.getenv("SESSION_ID_2")
GROUP_IDS = os.getenv("GROUP_IDS", "")
GROUP_TITLES = os.getenv("GROUP_TITLES", "")
MESSAGE_TEXT = os.getenv("MESSAGE_TEXT", "Hello 👋")
SELF_URL = os.getenv("SELF_URL", "")

# Timing
MSG_DELAY_BETWEEN_ACCOUNTS = 20        # 20 sec gap between acc1 and acc2
TITLE_DELAY_BETWEEN_ACCOUNTS = 120     # 2 min gap between acc1 and acc2
MSG_REFRESH_DELAY = 30
BURST_COUNT = 3
SELF_PING_INTERVAL = 60
COOLDOWN_ON_ERROR = 300
DOC_ID = os.getenv("DOC_ID", "29088580780787855")
CSRF_TOKEN = os.getenv("CSRF_TOKEN", "")

# --- Helper Functions ---
def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def build_headers(thread_id):
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "X-CSRFToken": CSRF_TOKEN,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://www.instagram.com/direct/t/{thread_id}/"
    }

def build_cookies(sessionid):
    return {"csrftoken": CSRF_TOKEN, "sessionid": sessionid}

def send_message(cl, gid, msg):
    try:
        cl.direct_send(msg, thread_ids=[int(gid)])
        log(f"✅ {cl.username} sent message to {gid}")
        return True
    except Exception as e:
        log(f"⚠ {cl.username} failed sending to {gid}: {e}")
        return False

def change_title(cl, thread_id, title):
    try:
        headers = build_headers(thread_id)
        cookies = build_cookies(cl.private.session.cookies.get("sessionid", ""))
        cl.private.headers.update(headers)
        cl.private.cookies.update(cookies)
        variables = {"thread_fbid": thread_id, "new_title": title}
        payload = {"doc_id": DOC_ID, "variables": json.dumps(variables)}
        r = cl.private.post("https://www.instagram.com/api/graphql/", data=payload)
        res = r.json()
        if "errors" in res:
            log(f"❌ Title change failed for {thread_id}: {res['errors']}")
        else:
            log(f"📝 {cl.username} changed group title → {title}")
    except Exception as e:
        log(f"⚠ {cl.username} title change error: {e}")

def self_ping():
    while True:
        if SELF_URL:
            try:
                requests.get(SELF_URL, timeout=10)
                log("🔁 Self ping done (Render active).")
            except Exception as e:
                log(f"⚠ Self ping error: {e}")
        time.sleep(SELF_PING_INTERVAL)

# --- Messaging Alternation ---
def message_cycle(c1, c2, groups):
    while True:
        # Account 1 sends
        for gid in groups:
            log(f"💬 {c1.username} sending to {gid}")
            for _ in range(BURST_COUNT):
                send_message(c1, gid, MESSAGE_TEXT)
                time.sleep(MSG_REFRESH_DELAY)
        log(f"⏳ Wait {MSG_DELAY_BETWEEN_ACCOUNTS}s before Account 2")
        time.sleep(MSG_DELAY_BETWEEN_ACCOUNTS)

        # Account 2 sends
        for gid in groups:
            log(f"💬 {c2.username} sending to {gid}")
            for _ in range(BURST_COUNT):
                send_message(c2, gid, MESSAGE_TEXT)
                time.sleep(MSG_REFRESH_DELAY)
        log(f"⏳ Wait {MSG_DELAY_BETWEEN_ACCOUNTS}s before Account 1")
        time.sleep(MSG_DELAY_BETWEEN_ACCOUNTS)

# --- Title Alternation ---
def title_cycle(c1, c2, titles_map, groups):
    while True:
        # Account 1 changes
        for gid in groups:
            titles = titles_map.get(str(gid), ["Default Title"])
            for t in titles:
                change_title(c1, gid, t)
                time.sleep(TITLE_DELAY_BETWEEN_ACCOUNTS)
        log("🕓 Account 1 done, waiting for Account 2")

        # Account 2 changes
        for gid in groups:
            titles = titles_map.get(str(gid), ["Default Title"])
            for t in titles:
                change_title(c2, gid, t)
                time.sleep(TITLE_DELAY_BETWEEN_ACCOUNTS)
        log("🕓 Account 2 done, waiting for Account 1")

# --- Start Bot ---
def start_bot():
    if not (SESSION_ID_1 and SESSION_ID_2):
        log("❌ Both SESSION_ID_1 and SESSION_ID_2 are required.")
        return

    groups = [g.strip() for g in GROUP_IDS.split(",") if g.strip()]
    if not groups:
        log("❌ No groups provided.")
        return

    titles_map = {}
    if GROUP_TITLES:
        try:
            titles_map = json.loads(GROUP_TITLES)
        except Exception as e:
            log(f"⚠ Failed to parse GROUP_TITLES: {e}")

    # Login both accounts
    c1 = Client()
    c2 = Client()
    try:
        c1.login_by_sessionid(SESSION_ID_1)
        log(f"✅ Logged in as {c1.username} (Account 1)")
        c2.login_by_sessionid(SESSION_ID_2)
        log(f"✅ Logged in as {c2.username} (Account 2)")
    except Exception as e:
        log(f"❌ Login error: {e}")
        return

    threading.Thread(target=message_cycle, args=(c1, c2, groups), daemon=True).start()
    threading.Thread(target=title_cycle, args=(c1, c2, titles_map, groups), daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()

@app.route("/")
def home_route():
    return "✅ Dual Account IG Bot running — Render Safe."

# Start automatically on render
threading.Thread(target=start_bot, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)
