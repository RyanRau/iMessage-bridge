"""
List all chats as seen by the Messages AppleScript API.
Use the 'id' column when setting 'applescript_id' in chats.json for group chat sends.

Usage:
    python helpers/list_applescript_chats.py
"""

import subprocess

script = '''
tell application "Messages"
    set output to ""
    repeat with c in every chat
        set cID to id of c
        set cName to ""
        try
            set cName to name of c
        end try
        set output to output & cID & "\t" & cName & "\n"
    end repeat
    return output
end tell'''

result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
if result.returncode != 0:
    print(f"Error: {result.stderr.strip()}")
    raise SystemExit(1)

lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
print(f"{'ID':<60} {'Name'}")
print("-" * 80)
for line in sorted(lines):
    parts = line.split("\t", 1)
    cid = parts[0].strip()
    name = parts[1].strip() if len(parts) > 1 else ""
    print(f"{cid:<60} {name}")
