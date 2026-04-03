import sys
import base64
import json
import os
import re
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

PORT = int(os.getenv("PORT", 5000))
HOST = os.getenv("HOST", "127.0.0.1")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 2))
HOST_HANDLE = os.getenv("HOST_HANDLE", "").strip()
DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")
DB_URI = f"file:{DB_PATH}?mode=ro"

_SCRIPT_DIR = os.path.dirname(sys.executable) if getattr(
    sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
CHATS_CONFIG_PATH = os.path.join(_SCRIPT_DIR, "chats.json")

app = Flask(__name__)

last_rowid = 0
chat_meta_by_identifier: dict = {}


def load_chats_config(path: str) -> list:
    if not os.path.exists(path):
        raise SystemExit(
            f"chats.json not found at {path}\n"
            "Copy chats.json.example to chats.json and configure your chats."
        )
    with open(path) as f:
        config = json.load(f)
    default_url = config.get("default_webhook_url")
    result = []
    for entry in config.get("chats", []):
        identifier = entry.get("chat_identifier", "").strip()
        if not identifier:
            continue
        webhook_url = entry.get("webhook_url") or default_url
        if not webhook_url:
            print(
                f"[warn] no webhook_url for {identifier!r} and no default — will skip posting")
        result.append({
            "chat_identifier": identifier,
            "webhook_url": webhook_url,
            "applescript_id": entry.get("applescript_id", "").strip() or None,
            "mention_only": bool(entry.get("mention_only", False)),
        })
    return result


def get_connection():
    return sqlite3.connect(DB_URI, uri=True)


def resolve_chat_rowids(chat_configs: list) -> dict:
    """Returns {rowid: {chat_identifier, display_name, style, is_group, webhook_url}}"""
    mapping = {}
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        for cfg in chat_configs:
            identifier = cfg["chat_identifier"]
            row = conn.execute(
                "SELECT ROWID, chat_identifier, display_name, style FROM chat WHERE chat_identifier = ? LIMIT 1",
                (identifier,),
            ).fetchone()
            if row is None:
                print(f"[warn] no chat found for: {identifier!r}")
            else:
                is_group = row["style"] == 43 or bool(row["display_name"])
                mapping[row["ROWID"]] = {
                    "chat_identifier": identifier,
                    "display_name": row["display_name"],
                    "style": row["style"],
                    "is_group": is_group,
                    "webhook_url": cfg["webhook_url"],
                    "applescript_id": cfg.get("applescript_id"),
                    "mention_only": cfg.get("mention_only", False),
                }
    return mapping


def seed_last_rowid():
    with get_connection() as conn:
        row = conn.execute("SELECT MAX(ROWID) FROM message").fetchone()
    return row[0] or 0


def normalize_phone(raw: str) -> str:
    return re.sub(r"\D", "", raw)


def build_contact_cache() -> dict:
    """Query macOS Contacts via osascript and return {handle -> display_name}."""
    script = '''
tell application "Contacts"
    set output to ""
    repeat with p in people
        set pName to name of p
        repeat with ph in phones of p
            set output to output & (value of ph) & "\t" & pName & "\n"
        end repeat
        repeat with em in emails of p
            set output to output & (value of em) & "\t" & pName & "\n"
        end repeat
    end repeat
    return output
end tell'''
    result = subprocess.run(["osascript", "-e", script],
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[contacts] could not load contacts: {result.stderr.strip()}")
        return {}
    cache = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        value, name = parts[0].strip(), parts[1].strip()
        if not value or not name:
            continue
        cache[value] = name
        normalized = normalize_phone(value)
        if normalized:
            cache[normalized] = name
    print(f"[contacts] loaded {len(cache)} entries")
    return cache


def resolve_sender_name(sender: str, cache: dict) -> str | None:
    if not sender or not cache:
        return None
    if sender in cache:
        return cache[sender]
    return cache.get(normalize_phone(sender))


def extract_text(text_col, attributed_body):
    if text_col:
        return text_col
    if attributed_body is None:
        return ""
    raw = bytes(attributed_body)
    # Binary plist (NSKeyedArchiver)
    if raw[:8] == b"bplist00":
        try:
            plist = plistlib.loads(raw)
            objects = plist.get("$objects", [])
            for obj in objects:
                if isinstance(obj, str) and obj != "$null":
                    return obj
        except Exception:
            pass
        return ""
    # Typedstream: skip known boilerplate and return first readable string
    _TS_SKIP = {
        "streamtyped", "NSString", "NSMutableString", "NSAttributedString",
        "NSMutableAttributedString", "NSDictionary", "NSMutableDictionary",
        "NSObject", "NSArray", "NSMutableArray", "NSColor", "NSFont",
        "NSParagraphStyle", "NSMutableParagraphStyle",
    }
    text = raw.decode("latin-1")
    for m in re.finditer(r"[\x20-\x7e]{2,}", text):
        s = m.group()
        if s not in _TS_SKIP and not s.startswith("__k") and not s.startswith("NS"):
            return s
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
    raw = bytes(attributed_body)

    # Binary plist (NSKeyedArchiver) — newer macOS
    if raw[:8] == b"bplist00":
        try:
            plist = plistlib.loads(raw)
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

    # Typedstream (NSUnarchiver / legacy format) — scan for Apple ID patterns
    if b"__kIMMentionConfirmedMention" not in raw:
        return []
    text = raw.decode("latin-1")
    emails = re.findall(
        r"[a-zA-Z0-9.+_%\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", text)
    phones = re.findall(r"\+\d{10,15}", text)
    return list(dict.fromkeys(emails + phones))


def poll(chat_rowid_map: dict, contact_cache: dict):
    global last_rowid
    target_chat_ids = set(chat_rowid_map.keys())

    while True:
        try:
            with get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT m.ROWID, m.text, m.attributedBody, m.is_from_me, m.date,
                           h.id as sender, cmj.chat_id, m.guid
                    FROM message m
                    JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
                    LEFT JOIN handle h ON m.handle_id = h.ROWID
                    WHERE m.ROWID > ?
                    ORDER BY m.ROWID ASC
                    """,
                    (last_rowid,),
                ).fetchall()

            for row in rows:
                rowid, text_col, attributed_body, is_from_me, date, sender, chat_id, guid = row
                text = extract_text(text_col, attributed_body)
                last_rowid = rowid

                if not text or chat_id not in target_chat_ids or is_from_me:
                    continue

                meta = chat_rowid_map[chat_id]

                try:
                    timestamp = apple_timestamp_to_datetime(date).isoformat()
                except Exception:
                    timestamp = None

                mentions = extract_mentions(attributed_body)
                mentioned = bool(HOST_HANDLE and HOST_HANDLE in mentions)
                sender_name = resolve_sender_name(sender, contact_cache)

                with get_connection() as conn2:
                    attachments = fetch_attachments(conn2, rowid)

                log_attachments = [
                    {**a,
                        "data": f"<{len(a['data'])} chars>" if a.get("data") else None}
                    for a in attachments
                ]
                log_payload = {
                    "rowid": rowid,
                    "guid": guid,
                    "chat_identifier": meta["chat_identifier"],
                    "chat_name": meta["display_name"],
                    "is_group": meta["is_group"],
                    "text": text,
                    "timestamp": timestamp,
                    "sender": sender or "unknown",
                    "sender_name": sender_name,
                    "mentioned": mentioned,
                    "attachments": log_attachments,
                }
                print(
                    f"[recv] chat={meta['chat_identifier']} from={sender or 'unknown'}\n{json.dumps(log_payload, indent=2)}")

                webhook_url = meta["webhook_url"]
                if webhook_url:
                    if meta["mention_only"] and not mentioned:
                        print(f"[webhook] skipping (mention_only) chat={meta['chat_identifier']}")
                    else:
                        payload = {**log_payload, "attachments": attachments}
                        try:
                            requests.post(webhook_url, json=payload, timeout=5)
                        except Exception as e:
                            print(f"[webhook] failed: {e}")
                else:
                    print(
                        f"[webhook] no URL for {meta['chat_identifier']}, skipping")

        except Exception as e:
            print(f"[poll] error: {e}")

        time.sleep(POLL_INTERVAL)


def send_imessage(recipient: str, message: str, is_group: bool = False, applescript_id: str | None = None):
    safe_msg = message.replace("\\", "\\\\").replace('"', '\\"')

    if is_group:
        chat_id = applescript_id or recipient
        safe_chat_id = chat_id.replace("\\", "\\\\").replace('"', '\\"')
        script = f'''
tell application "Messages"
    set targetChat to first chat whose id is "{safe_chat_id}"
    send "{safe_msg}" to targetChat
end tell'''
    else:
        safe_recipient = recipient.replace("\\", "\\\\").replace('"', '\\"')
        script = f'''
tell application "Messages"
    set targetService to 1st service whose service type = iMessage
    set targetBuddy to buddy "{safe_recipient}" of targetService
    send "{safe_msg}" to targetBuddy
end tell'''

    subprocess.run(["osascript", "-e", script],
                   check=True, capture_output=True)


@app.route("/send", methods=["POST"])
def send():
    data = request.get_json(force=True, silent=True) or {}
    message = data.get("message", "").strip()
    recipient = data.get("recipient", "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400
    if not recipient:
        return jsonify({"error": "recipient is required"}), 400
    if recipient not in chat_meta_by_identifier:
        return jsonify({"error": "recipient not in allowed targets", "allowed": list(chat_meta_by_identifier)}), 403
    meta = chat_meta_by_identifier[recipient]
    try:
        send_imessage(recipient, message, is_group=meta["is_group"], applescript_id=meta.get("applescript_id"))
        print(
            f"[send] to={recipient} | is_group={meta['is_group']} | text={message!r}")
        return jsonify({"status": "sent", "recipient": recipient})
    except subprocess.CalledProcessError as e:
        return jsonify({"error": e.stderr.decode(errors="replace")}), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    check_permissions()

    chat_configs = load_chats_config(CHATS_CONFIG_PATH)
    chat_rowid_map = resolve_chat_rowids(chat_configs)
    if not chat_rowid_map:
        raise SystemExit(
            "No valid chats resolved. Check chat identifiers in chats.json.")

    chat_meta_by_identifier = {
        meta["chat_identifier"]: meta for meta in chat_rowid_map.values()
    }
    print(f"Monitoring: {list(chat_meta_by_identifier.keys())}")

    contact_cache = build_contact_cache()

    last_rowid = seed_last_rowid()

    t = threading.Thread(target=poll, args=(
        chat_rowid_map, contact_cache), daemon=True)
    t.start()
    print(f"Polling started on {HOST}:{PORT}")

    from waitress import serve
    serve(app, host=HOST, port=PORT)
