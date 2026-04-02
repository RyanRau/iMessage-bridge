# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

A macOS-only Python web server that bridges iMessage conversations to external webhooks. It polls `~/Library/Messages/chat.db` (SQLite, read-only) for new messages and POSTs them to per-chat configurable webhook URLs. It also exposes a `/send` endpoint to send replies back via AppleScript.

## Setup & Running

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in optional settings
cp chats.json.example chats.json  # configure chats and webhook URLs
python server.py
```

List available chat identifiers (needed for `chats.json`):
```bash
python helpers/list_chats.py [limit]
```

Build standalone macOS binary (done automatically by CI on `v*` tags):
```bash
pyinstaller --onefile --name imessage-bridge server.py
```

There are no linting or testing frameworks configured.

## Architecture

Everything lives in `server.py`. Key sections:

**Startup**: `check_permissions()` verifies Full Disk Access (to read `chat.db`) and AppleScript automation access. `load_chats_config()` reads `chats.json`. `build_contact_cache()` queries macOS Contacts via osascript and builds an in-memory phone/email → name mapping. Exits with clear messages on misconfiguration.

**Polling thread** (`poll(chat_rowid_map, contact_cache)`): Daemon thread running every `POLL_INTERVAL` seconds. Queries `ROWID > last_rowid`, routes each message to its per-chat webhook URL.

**Message extraction helpers**:
- `extract_text()` — handles plain text and Apple's attributedBody (binary plist and typedstream formats)
- `extract_mentions()` — parses attributedBody for @mentions, compares against `HOST_HANDLE`
- `fetch_attachments()` — queries attachment metadata, base64-encodes images
- `apple_timestamp_to_datetime()` — converts Apple epoch (Jan 1, 2001, optionally nanoseconds) to ISO8601

**Contact resolution**:
- `build_contact_cache()` — osascript query to Contacts app, returns `{handle: name}` dict
- `normalize_phone()` — strips non-digit chars for fuzzy phone matching
- `resolve_sender_name()` — exact match then normalized fallback

**Flask endpoints**:
- `GET /health` — returns `{"status": "ok"}`
- `POST /send` — sends a message via AppleScript; body: `{"message": "...", "recipient": "..."}`. Automatically uses group chat AppleScript for group chats. Recipient must be a `chat_identifier` from `chats.json`.

## Configuration

**`chats.json`** (required) — chat routing config:
```json
{
  "default_webhook_url": "https://n8n.example.com/webhook/default",
  "chats": [
    { "chat_identifier": "chat012345678901234", "webhook_url": "https://n8n.example.com/webhook/group" },
    { "chat_identifier": "+15551234567", "webhook_url": "https://n8n.example.com/webhook/personal" },
    { "chat_identifier": "+15559876543" }
  ]
}
```
Entries without `webhook_url` fall back to `default_webhook_url`. If neither is set, the message is processed but not POSTed.

**`.env`** (optional settings):

| Variable | Default | Description |
|---|---|---|
| `HOST` | `127.0.0.1` | Flask listen address |
| `PORT` | `5000` | Flask listen port |
| `POLL_INTERVAL` | `2` | Seconds between database polls |
| `HOST_HANDLE` | optional | Your phone/Apple ID, used for `mentioned` detection |

## Webhook Payload Shape

```json
{
  "rowid": 12345,
  "guid": "uuid-string",
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
      "data": "base64-or-null"
    }
  ]
}
```

## Database Schema (read-only)

Relevant tables in `~/Library/Messages/chat.db`:
- `message` — `text`, `attributedBody`, `is_from_me`, `date`, `guid`, `handle_id`
- `handle` — contact identifiers (`id` = phone number or Apple ID)
- `chat` — `chat_identifier`, `display_name`, `service_name`, `style` (`style=43` = group chat)
- `chat_message_join` — maps messages to chats
- `attachment` + `message_attachment_join` — file/image metadata
