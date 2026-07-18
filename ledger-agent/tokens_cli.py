"""Bearer token management for ledger_mcp.

  python3 tokens_cli.py create --label "claude-code laptop" --user alta
  python3 tokens_cli.py create --label "charlee phone" --user charlee --scopes read
  python3 tokens_cli.py list
  python3 tokens_cli.py revoke 3

The raw token is printed ONCE at creation and never stored — only its
SHA-256 hash lands in api_tokens. Scopes: 'read' now; 'read,write' becomes
meaningful when the write tier ships.
"""
import argparse
import secrets
import sys
from datetime import datetime

from ledger_core import connect_rw, hash_token


def cmd_create(args) -> int:
    conn = connect_rw()
    try:
        user_id = None
        if args.user:
            r = conn.execute(
                "SELECT id FROM users WHERE LOWER(username)=LOWER(?) "
                "OR LOWER(display_name)=LOWER(?)",
                (args.user, args.user)).fetchone()
            if not r:
                print(f"No user matching '{args.user}'.", file=sys.stderr)
                return 1
            user_id = r["id"]
        token = "lgr_" + secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO api_tokens (token_hash, label, user_id, scopes, "
            "created_at) VALUES (?,?,?,?,?)",
            (hash_token(token), args.label, user_id, args.scopes,
             datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        print("Token created. Copy it now — it is not stored and cannot "
              "be shown again:\n")
        print(f"  {token}\n")
        print(f"  label:  {args.label}")
        print(f"  scopes: {args.scopes}")
        return 0
    finally:
        conn.close()


def cmd_list(_args) -> int:
    conn = connect_rw()
    try:
        rows = conn.execute(
            "SELECT id, label, scopes, created_at, last_used_at, revoked "
            "FROM api_tokens ORDER BY id").fetchall()
        if not rows:
            print("No tokens.")
            return 0
        for r in rows:
            status = "REVOKED" if r["revoked"] else "active"
            print(f"[{r['id']}] {r['label']}  ({r['scopes']})  {status}  "
                  f"created {r['created_at']}  "
                  f"last used {r['last_used_at'] or 'never'}")
        return 0
    finally:
        conn.close()


def cmd_revoke(args) -> int:
    conn = connect_rw()
    try:
        n = conn.execute(
            "UPDATE api_tokens SET revoked = 1 WHERE id = ?",
            (args.id,)).rowcount
        conn.commit()
        print("Revoked." if n else f"No token with id {args.id}.")
        return 0 if n else 1
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--label", required=True)
    c.add_argument("--user", help="username or display name to attribute")
    c.add_argument("--scopes", default="read")
    c.set_defaults(fn=cmd_create)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    r = sub.add_parser("revoke")
    r.add_argument("id", type=int)
    r.set_defaults(fn=cmd_revoke)
    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
