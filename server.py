import base64
import os
import sqlite3
import threading
import time
import plistlib
import subprocess
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv


def check_permissions():
    # Full Disk Access — needed to read chat.db
    try:
        open(os.path.expanduser("~/Library/Messages/chat.db"), "rb").close()
    except (PermissionError, OSError):
        print("Full Disk Access is required to read Messages.")
        print("Opening System Settings — grant access for this app, then relaunch.")
        subprocess.run([
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
        ])
        raise SystemExit(1)

    # Automation (Messages) — trigger the permission prompt now rather than mid-request
    result = subprocess.run(
        ["osascript", "-e", 'tell application "Messages" to get name'],
        capture_output=True
    )
    if result.returncode != 0:
        print("Automation access for Messages is required to send messages.")
        print("Please allow access when prompted, then relaunch.")
        raise SystemExit(1)

load_dotenv()

# CHAT_TARGETS is a comma-separated list of phone numbers / Apple IDs
CHAT_TARGETS = [t.strip() for t in os.environ["CHAT_TARGET"].split(",")]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
PORT = int(os.getenv("PORT", 5000))
HOST = os.getenv("HOST", "127.0.0.1")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 2))
HOST_HANDLE = os.getenv("HOST_HANDLE", "").strip()
DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")
DB_URI = f"file:{DB_PATH}?mode=ro"

app = Flask(__name__)

last_rowid = 0


def get_connection():
    return sqlite3.connect(DB_URI, uri=True)


def resolve_chat_rowids():
    mapping = {}
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        for target in CHAT_TARGETS:
            row = conn.execute(
                "SELECT * FROM chat WHERE chat_identifier = ? LIMIT 1",
                (target,),
            ).fetchone()
            if row is None:
                print(f"[warn] no chat found for target: {target!r}")
            else:
                mapping[target] = row["ROWID"]
    return mapping


def seed_last_rowid():
    with get_connection() as conn:
        row = conn.execute("SELECT MAX(ROWID) FROM message").fetchone()
    return row[0] or 0


def extract_text(text_col, attributed_body):
    if text_col:
        return text_col
    if attributed_body is None:
        return ""
    try:
        plist = plistlib.loads(bytes(attributed_body))
        objects = plist.get("$objects", [])
        for obj in objects:
            if isinstance(obj, str) and obj != "$null":
                return obj
    except Exception:
        pass
    return ""


def apple_timestamp_to_datetime(ts):
    if ts > 1e15:
        ts = ts / 1e9
    apple_epoch = 978307200
    unix_ts = ts + apple_epoch
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).replace(tzinfo=None)


def fetch_attachments(conn, message_rowid: int) -> list:
    rows = conn.execute(
        """
        SELECT a.filename, a.mime_type, a.transfer_name, a.total_bytes
        FROM message_attachment_join maj
        JOIN attachment a ON a.ROWID = maj.attachment_id
        WHERE maj.message_id = ?
        """,
        (message_rowid,),
    ).fetchall()
    result = []
    for filename, mime_type, transfer_name, total_bytes in rows:
        entry = {
            "filename": filename,
            "mime_type": mime_type,
            "transfer_name": transfer_name,
            "total_bytes": total_bytes,
        }
        if mime_type and mime_type.startswith("image/"):
            try:
                path = os.path.expanduser(filename)
                with open(path, "rb") as f:
                    entry["data"] = base64.b64encode(f.read()).decode()
            except OSError as e:
                print(f"[attachment] could not read {filename}: {e}")
                entry["data"] = None
        result.append(entry)
    return result


def extract_mentions(attributed_body) -> list:
    if attributed_body is None:
        return []
    try:
        plist = plistlib.loads(bytes(attributed_body))
        objects = plist.get("$objects", [])
        mentions = []
        for obj in objects:
            if isinstance(obj, dict):
                value = obj.get("__kIMMentionConfirmedMention")
                if value is None:
                    continue
                if isinstance(value, plistlib.UID):
                    value = objects[value.integer]
                if isinstance(value, str):
                    mentions.append(value)
        return mentions
    except Exception:
        return []


def fetch_reply_chain(conn, reply_to_guid, max_depth: int = 5) -> list:
    if not reply_to_guid:
        return []
    chain = []
    current_guid = reply_to_guid
    for _ in range(max_depth):
        row = conn.execute(
            """
            SELECT m.guid, m.text, m.attributedBody, m.is_from_me, m.date,
                   m.reply_to_guid, h.id as sender
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.guid = ?
            """,
            (current_guid,),
        ).fetchone()
        if row is None:
            break
        guid, text_col, attributed_body, is_from_me, date, next_guid, sender = row
        text = extract_text(text_col, attributed_body)
        try:
            timestamp = apple_timestamp_to_datetime(date).isoformat()
        except Exception:
            timestamp = None
        chain.append({
            "guid": guid,
            "text": text,
            "sender": sender or "unknown",
            "is_from_me": bool(is_from_me),
            "timestamp": timestamp,
        })
        if not next_guid:
            break
        current_guid = next_guid
    chain.reverse()
    return chain


def poll(chat_rowid_map):
    global last_rowid
    target_chat_ids = set(chat_rowid_map.values())

    while True:
        try:
            with get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT m.ROWID, m.text, m.attributedBody, m.is_from_me, m.date,
                           h.id as sender, cmj.chat_id, m.guid, m.reply_to_guid
                    FROM message m
                    JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
                    LEFT JOIN handle h ON m.handle_id = h.ROWID
                    WHERE m.ROWID > ?
                    ORDER BY m.ROWID ASC
                    """,
                    (last_rowid,),
                ).fetchall()

            for row in rows:
                rowid, text_col, attributed_body, is_from_me, date, sender, chat_id, guid, reply_to_guid = row
                text = extract_text(text_col, attributed_body)
                last_rowid = rowid

                if not text or chat_id not in target_chat_ids:
                    continue

                try:
                    timestamp = apple_timestamp_to_datetime(date).isoformat()
                except Exception:
                    timestamp = None

                direction = "me" if is_from_me else (sender or "unknown")
                print(f"[recv] from={direction} | time={timestamp} | text={text!r}")

                mentions = extract_mentions(attributed_body)
                mentioned = bool(HOST_HANDLE and HOST_HANDLE in mentions)

                with get_connection() as conn2:
                    attachments = fetch_attachments(conn2, rowid)
                    reply_chain = fetch_reply_chain(conn2, reply_to_guid)

                payload = {
                    "rowid": rowid,
                    "guid": guid,
                    "text": text,
                    "is_from_me": bool(is_from_me),
                    "timestamp": timestamp,
                    "sender": sender or "unknown",
                    "mentioned": mentioned,
                    "attachments": attachments,
                    "reply_chain": reply_chain,
                }
                try:
                    requests.post(WEBHOOK_URL, json=payload, timeout=5)
                except Exception as e:
                    print(f"[webhook] failed: {e}")

        except Exception as e:
            print(f"[poll] error: {e}")

        time.sleep(POLL_INTERVAL)


def send_imessage(recipient: str, message: str):
    safe = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
tell application "Messages"
    set targetService to 1st service whose service type = iMessage
    set targetBuddy to buddy "{recipient}" of targetService
    send "{safe}" to targetBuddy
end tell'''
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True)


@app.route("/send", methods=["POST"])
def send():
    data = request.get_json(force=True, silent=True) or {}
    message = data.get("message", "").strip()
    recipient = data.get("recipient", CHAT_TARGETS[0]).strip()
    if not message:
        return jsonify({"error": "message is required"}), 400
    if recipient not in CHAT_TARGETS:
        return jsonify({"error": "recipient not in allowed targets", "allowed": CHAT_TARGETS}), 403
    try:
        send_imessage(recipient, message)
        print(f"[send] to={recipient} | text={message!r}")
        return jsonify({"status": "sent", "recipient": recipient})
    except subprocess.CalledProcessError as e:
        return jsonify({"error": e.stderr.decode(errors="replace")}), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    check_permissions()
    chat_rowid_map = resolve_chat_rowids()
    if not chat_rowid_map:
        raise SystemExit("No valid targets resolved. Check CHAT_TARGET in .env")
    print(f"Monitoring: {list(chat_rowid_map.keys())}")

    last_rowid = seed_last_rowid()

    t = threading.Thread(target=poll, args=(chat_rowid_map,), daemon=True)
    t.start()
    print(f"Polling started on {HOST}:{PORT}")

    app.run(host=HOST, port=PORT)
