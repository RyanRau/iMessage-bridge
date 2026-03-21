import sqlite3
import os
import sys
from datetime import datetime, timezone

limit = int(sys.argv[1]) if len(sys.argv) > 1 else 10

DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")
DB_URI = f"file:{DB_PATH}?mode=ro"

conn = sqlite3.connect(DB_URI, uri=True)
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT
        c.ROWID,
        c.chat_identifier,
        c.display_name,
        c.service_name,
        c.style,
        MAX(m.date) as last_date
    FROM chat c
    JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
    JOIN message m ON cmj.message_id = m.ROWID
    GROUP BY c.ROWID
    ORDER BY last_date DESC
    LIMIT ?
""", (limit,)).fetchall()

conn.close()

def fmt_date(ts):
    if not ts:
        return "unknown"
    if ts > 1e15:
        ts = ts / 1e9
    return datetime.fromtimestamp(ts + 978307200, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

print(f"{'#':<3} {'type':<7} {'last message':<18} {'display name':<25} {'chat_identifier (use in CHAT_TARGET)'}")
print("-" * 100)
for i, row in enumerate(rows, 1):
    kind = "group" if row["style"] == 43 or row["display_name"] else "1:1"
    label = row["display_name"] or ""
    print(f"{i:<3} {kind:<7} {fmt_date(row['last_date']):<18} {label:<25} {row['chat_identifier']}")
