# iMessage Bridge

A macOS-only Python server that monitors iMessage conversations and bridges them to external webhooks. Polls `~/Library/Messages/chat.db` directly for new messages and POSTs them to per-chat configurable URLs. Also exposes a `/send` endpoint to send replies back via AppleScript.

## Requirements

- macOS (reads the local iMessage SQLite database)
- Python 3.12+
- Two macOS permissions:
  - **Full Disk Access** — to read `~/Library/Messages/chat.db`
    > System Settings → Privacy & Security → Full Disk Access → add Terminal (or your Python binary)
  - **Automation → Messages** — to send messages via AppleScript
    > System Settings → Privacy & Security → Automation → enable Messages for Terminal
  - **Contacts** (optional) — for sender name resolution; prompted automatically on first run

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
cp chats.json.example chats.json
```

Edit `chats.json` to configure which chats to monitor and where to route their webhooks. Use the helper to find chat identifiers:

```bash
python helpers/list_chats.py
```

Then start the server:

```bash
python server.py
```

## Configuration

### `chats.json`

Controls which chats are monitored and where messages are routed:

```json
{
  "default_webhook_url": "https://n8n.example.com/webhook/default",
  "chats": [
    {
      "chat_identifier": "chat012345678901234",
      "webhook_url": "https://n8n.example.com/webhook/groupchat",
      "applescript_id": "any;+;4ea9921929b142078cb49b7788edba8b",
      "mention_only": true
    },
    {
      "chat_identifier": "+15551234567",
      "webhook_url": "https://n8n.example.com/webhook/personal"
    },
    {
      "chat_identifier": "+15559876543"
    }
  ]
}
```

- `chat_identifier` — the value from `helpers/list_chats.py`
- `webhook_url` — per-chat destination; falls back to `default_webhook_url` if omitted
- `applescript_id` — required for group chats to send replies; get this value by running `python helpers/list_applescript_chats.py`
- `mention_only` — if `true`, only fires the webhook when `HOST_HANDLE` is @mentioned; requires `HOST_HANDLE` to be set in `.env`
- If neither `webhook_url` nor `default_webhook_url` is set for a chat, the message is processed but not POSTed

### `.env`

| Variable | Default | Description |
|---|---|---|
| `HOST` | `127.0.0.1` | Flask listen address |
| `PORT` | `5000` | Flask listen port |
| `POLL_INTERVAL` | `2` | Seconds between database polls |
| `HOST_HANDLE` | — | Your phone/Apple ID for `mentioned` detection |

## Webhook Payload

When a new message arrives, the server POSTs JSON to the configured webhook URL:

```json
{
  "rowid": 12345,
  "guid": "CA656C7E-B1C7-47E7-9659-AFB860D5E7B7",
  "chat_identifier": "chat012345678901234",
  "chat_name": "My Group Chat",
  "is_group": true,
  "text": "Hello!",
  "is_from_me": false,
  "timestamp": "2026-03-20T12:34:56",
  "sender": "+15551234567",
  "sender_name": "John Smith",
  "mentioned": false,
  "attachments": [
    {
      "filename": "/path/to/image.jpg",
      "mime_type": "image/jpeg",
      "transfer_name": "image.jpg",
      "total_bytes": 102400,
      "data": "<base64-encoded or null>"
    }
  ]
}
```

- `chat_name` — display name for group chats, `null` for 1:1
- `sender_name` — resolved from macOS Contacts, `null` if not found
- `mentioned` — `true` if `HOST_HANDLE` was @mentioned in the message
- `attachments[].data` — base64-encoded for images; `null` for other file types

## API

### `GET /health`

```bash
curl http://localhost:5000/health
# {"status": "ok"}
```

### `POST /send`

Sends a message to a monitored chat. Automatically handles group vs 1:1 routing.

```bash
curl -X POST http://localhost:5000/send \
  -H "Content-Type: application/json" \
  -d '{"recipient": "+15551234567", "message": "Hello!"}'

# Group chat
curl -X POST http://localhost:5000/send \
  -H "Content-Type: application/json" \
  -d '{"recipient": "chat012345678901234", "message": "Hello group!"}'
```

`recipient` must be a `chat_identifier` configured in `chats.json`. Both fields are required.

## Helpers

```bash
# List recent chats with their identifiers (chat_identifier values for chats.json)
python helpers/list_chats.py [limit]

# List chats as seen by the Messages AppleScript API (applescript_id values for group chat sends)
python helpers/list_applescript_chats.py

# Dump raw message data for debugging (plist, mentions, etc.)
python helpers/dump_message.py <rowid>
python helpers/dump_message.py --last [N]
```

## Distribution

Tagged releases are automatically built as a standalone macOS binary via GitHub Actions (PyInstaller). Download from the releases page to run without a Python environment.
