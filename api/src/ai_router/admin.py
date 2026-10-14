"""Operator commands for the API's SQLite database - run inside the `api` container:

    docker compose exec api python -m ai_router.admin stats
    docker compose exec api python -m ai_router.admin backup /app/data/backup.sqlite3
    docker compose exec api python -m ai_router.admin retry-failed
    docker compose exec api python -m ai_router.admin forget jan.nowak@example.com

The running API keeps its own connection; the database is in WAL mode, so these commands can run
while the API is serving requests. Output is JSON (easy to read and to script).
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

DEFAULT_DB = "/app/data/ai-router.sqlite3"
_TABLES_WITH_STATUS = ("tickets", "outbox")


def connect(path: str) -> sqlite3.Connection:
    if not Path(path).is_file():
        raise SystemExit(f"database not found: {path}")
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Counts by status, oldest ticket - a quick health check of the stored data."""
    result: dict[str, Any] = {}
    for table in _TABLES_WITH_STATUS:
        rows = conn.execute(f"SELECT status, COUNT(*) AS n FROM {table} GROUP BY status")  # noqa: S608
        result[table] = {row["status"]: row["n"] for row in rows}
    result["feedback"] = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    result["idempotency_keys"] = conn.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0]
    oldest = conn.execute("SELECT MIN(created_at) FROM tickets").fetchone()[0]
    result["oldest_ticket_age_days"] = round((time.time() - oldest) / 86400, 1) if oldest else None
    return result


def backup(conn: sqlite3.Connection, target: str) -> dict[str, Any]:
    """Consistent online copy (SQLite backup API) - safe while the API is writing."""
    destination = Path(target)
    if destination.exists():
        raise SystemExit(f"refusing to overwrite {target}")
    with sqlite3.connect(destination) as copy:
        conn.backup(copy)
    copy.close()
    return {"backup": str(destination), "bytes": destination.stat().st_size}


def retry_failed(conn: sqlite3.Connection) -> dict[str, Any]:
    """Give e-mails that exhausted their delivery attempts a fresh set of attempts."""
    cursor = conn.execute(
        "UPDATE outbox SET status = 'pending', attempts = 0, next_attempt_at = ? "
        "WHERE status = 'failed'",
        (time.time(),),
    )
    return {"requeued": cursor.rowcount}


def forget(conn: sqlite3.Connection, email: str) -> dict[str, Any]:
    """Erase one person's data (GDPR/RODO request): tickets, queued e-mails, feedback, replays.

    E-mails already delivered to department mailboxes (and Mailpit) are outside this database.
    """
    address = email.strip().lower()
    tickets = "SELECT ticket_id FROM tickets WHERE lower(sender_email) = ?"
    removed: dict[str, int] = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table in ("feedback", "outbox"):
            sql = f"DELETE FROM {table} WHERE ticket_id IN ({tickets})"  # noqa: S608
            removed[table] = conn.execute(sql, (address,)).rowcount
        removed["tickets"] = conn.execute(
            "DELETE FROM tickets WHERE lower(sender_email) = ?", (address,)
        ).rowcount
        # Stored responses for Idempotency-Key replays contain the Reply-To address.
        removed["idempotency"] = conn.execute(
            "DELETE FROM idempotency WHERE instr(lower(body), ?) > 0", (address,)
        ).rowcount
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"email": address, "removed": removed}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ai_router.admin", description=__doc__)
    parser.add_argument("--db", default=os.environ.get("DATABASE_PATH", DEFAULT_DB))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("stats", help="counts of tickets, outbox entries and feedback")
    commands.add_parser("backup", help="online backup to a new file").add_argument("target")
    commands.add_parser("retry-failed", help="re-queue e-mails that exhausted delivery attempts")
    commands.add_parser("forget", help="erase all data of one sender").add_argument("email")
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        if args.command == "stats":
            result = stats(conn)
        elif args.command == "backup":
            result = backup(conn, args.target)
        elif args.command == "retry-failed":
            result = retry_failed(conn)
        else:
            result = forget(conn, args.email)
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
