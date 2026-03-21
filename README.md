# iMessage Server

A lightweight Python server that monitors a single iMessage conversation and bridges it to an external webhook. When a message arrives, it POSTs the message to a configurable URL. Also exposes an endpoint to send replies back into the same chat.

## Setup

### macOS Permissions

Two permissions are required before the server will work:

**Full Disk Access** — needed to read `~/Library/Messages/chat.db`:
> System Settings → Privacy & Security → Full Disk Access → add Terminal (or your Python binary)

**Automation → Messages** — needed to send messages via AppleScript:
> System Settings → Privacy & Security → Automation → enable Messages for Terminal


### Run

```bash
python -m venv .venv

pip install -r requirements.txt

python server.py
```

## API

### `GET /health`

Returns `{"status": "ok"}` — useful for confirming the server is running.

### `POST /send`

Sends a message to the watched chat.

```bash
curl -X POST http://localhost:5000/send \
  -H 'Content-Type: application/json' \
  -d '{"message": "hello"}'
```

## Webhook

When a new message arrives in the watched chat, the server POSTs JSON to `WEBHOOK_URL`:

```json
{
  "rowid": 12345,
  "text": "Hello!",
  "is_from_me": false,
  "timestamp": "2026-03-20T12:34:56",
  "sender": "+15551234567"
}
```