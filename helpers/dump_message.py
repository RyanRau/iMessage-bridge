"""
Dump the raw attributedBody plist for a message to debug mention/text extraction.

Usage:
    python helpers/dump_message.py <rowid>
    python helpers/dump_message.py --last [N]   # dump last N messages (default 1)
"""

import os
import plistlib
import sqlite3
import sys

DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")


def get_connection():
    return sqlite3.connect(DB_PATH)


def dump_plist(plist, indent=0):
    pad = "  " * indent
    if isinstance(plist, dict):
        for k, v in plist.items():
            if isinstance(v, (dict, list)):
                print(f"{pad}{k!r}:")
                dump_plist(v, indent + 1)
            elif isinstance(v, bytes):
                print(f"{pad}{k!r}: <bytes len={len(v)}>")
            else:
                print(f"{pad}{k!r}: {v!r}")
    elif isinstance(plist, list):
        for i, item in enumerate(plist):
            if isinstance(item, (dict, list)):
                print(f"{pad}[{i}]:")
                dump_plist(item, indent + 1)
            elif isinstance(item, bytes):
                print(f"{pad}[{i}]: <bytes len={len(item)}>")
            else:
                print(f"{pad}[{i}]: {item!r}")
    else:
        print(f"{pad}{plist!r}")


def dump_message(rowid):
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT m.ROWID, m.text, m.attributedBody, m.is_from_me, m.date,
                   h.id as sender, m.guid
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.ROWID = ?
            """,
            (rowid,),
        ).fetchone()

    if row is None:
        print(f"No message found with ROWID={rowid}")
        return

    rowid, text_col, attributed_body, is_from_me, date, sender, guid = row
    print(f"=== Message ROWID={rowid} ===")
    print(f"  guid:        {guid}")
    print(f"  is_from_me:  {bool(is_from_me)}")
    print(f"  sender:      {sender}")
    print(f"  text_col:    {text_col!r}")
    print(f"  attributedBody: {'<None>' if attributed_body is None else f'<bytes len={len(attributed_body)}>'}")

    if attributed_body is not None:
        raw = bytes(attributed_body)
        print(f"\n--- Raw bytes (first 32) ---")
        print(f"  hex:    {raw[:32].hex()}")
        print(f"  ascii:  {raw[:32]!r}")

        try:
            plist = plistlib.loads(raw)
            print("\n--- Parsed plist ---")
            dump_plist(plist)

            print("\n--- Objects containing 'mention' (case-insensitive) ---")
            objects = plist.get("$objects", [])
            for i, obj in enumerate(objects):
                if isinstance(obj, dict):
                    for k in obj:
                        if "mention" in str(k).lower():
                            value = obj[k]
                            if isinstance(value, plistlib.UID):
                                resolved = objects[value.integer]
                                print(f"  objects[{i}][{k!r}] = UID({value.integer}) -> {resolved!r}")
                            else:
                                print(f"  objects[{i}][{k!r}] = {value!r}")
                elif isinstance(obj, str) and "mention" in obj.lower():
                    print(f"  objects[{i}] = {obj!r}")
        except Exception as e:
            print(f"\n  plistlib failed ({e})")
            print("\n--- Full hex dump ---")
            for off in range(0, len(raw), 16):
                chunk = raw[off:off + 16]
                hex_part = " ".join(f"{b:02x}" for b in chunk)
                asc_part = "".join(chr(b) if 0x20 <= b < 0x7f else "." for b in chunk)
                print(f"  {off:04x}  {hex_part:<47}  {asc_part}")

            print("\n--- All printable ASCII runs (len >= 3) across full stream ---")
            import re
            text = raw.decode("latin-1")
            for m in re.finditer(r"[\x20-\x7e]{3,}", text):
                print(f"  offset={m.start():4d}  {m.group()!r}")

            print("\n--- Bytes after __kIMMentionConfirmedMention key ---")
            key = b"__kIMMentionConfirmedMention"
            idx = raw.find(key)
            if idx != -1:
                after = raw[idx + len(key): idx + len(key) + 32]
                print(f"  hex: {after.hex()}")
                print(f"  repr: {after!r}")
    print()


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    if args[0] == "--last":
        n = int(args[1]) if len(args) > 1 else 1
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT ROWID FROM message ORDER BY ROWID DESC LIMIT ?", (n,)
            ).fetchall()
        for (rowid,) in reversed(rows):
            dump_message(rowid)
    else:
        dump_message(int(args[0]))


if __name__ == "__main__":
    main()
