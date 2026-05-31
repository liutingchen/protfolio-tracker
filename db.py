"""SQLite storage: users, portfolios, trades, and a shared price cache.

Multi-user: every portfolio belongs to a user; trades belong to portfolios.
Per-user UI state (active portfolio, view scope, combined-view mode) lives on
the users row. The price cache is shared across all users.
"""
import hashlib
import os
import secrets
import sqlite3
import time

from werkzeug.security import generate_password_hash

# DATA_DIR can be overridden (e.g. a Render persistent disk at /var/data).
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "portfolio.db")

DEMO_EMAIL = "demo@demo.com"
DEMO_PASSWORD = "demo1234"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    email               TEXT    UNIQUE NOT NULL,
    password_hash       TEXT    NOT NULL,
    active_portfolio_id INTEGER,
    view_scope          TEXT    NOT NULL DEFAULT 'single',
    all_display_mode    TEXT    NOT NULL DEFAULT 'value',
    email_verified      INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS portfolios (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER,
    name             TEXT    NOT NULL,
    starting_capital REAL    NOT NULL DEFAULT 0,
    display_mode     TEXT    NOT NULL DEFAULT 'value',
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS trades (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    date      TEXT    NOT NULL,
    ticker    TEXT    NOT NULL,
    side      TEXT    NOT NULL,
    shares    REAL    NOT NULL,
    price     REAL    NOT NULL,
    fees      REAL    NOT NULL DEFAULT 0,
    reason    TEXT    NOT NULL DEFAULT '',
    created_at TEXT   NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS price_cache (
    ticker TEXT NOT NULL,
    date   TEXT NOT NULL,
    close  REAL NOT NULL,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS price_meta (
    ticker       TEXT PRIMARY KEY,
    last_fetched TEXT,
    source       TEXT
);

CREATE TABLE IF NOT EXISTS email_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    kind       TEXT    NOT NULL,          -- 'verify' | 'reset'
    expires_at INTEGER NOT NULL,          -- epoch seconds
    used       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


def get_conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _column_exists(conn, table, column):
    return any(r["name"] == column
               for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        if not _column_exists(conn, "trades", "portfolio_id"):
            conn.execute("ALTER TABLE trades ADD COLUMN portfolio_id INTEGER")
        if not _column_exists(conn, "portfolios", "user_id"):
            conn.execute("ALTER TABLE portfolios ADD COLUMN user_id INTEGER")
        if not _column_exists(conn, "users", "email_verified"):
            conn.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE users SET email_verified = 1")  # grandfather existing accounts
        _migrate_orphans(conn)
        conn.commit()


def _migrate_orphans(conn):
    """Adopt pre-auth data (portfolios with no owner) into a demo account."""
    orphan = conn.execute(
        "SELECT COUNT(*) AS c FROM portfolios WHERE user_id IS NULL").fetchone()["c"]
    # also adopt trades that never got a portfolio_id (very old data)
    no_pf = conn.execute(
        "SELECT COUNT(*) AS c FROM trades WHERE portfolio_id IS NULL").fetchone()["c"]
    if not orphan and not no_pf:
        return

    old = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
    demo = conn.execute("SELECT id FROM users WHERE email = ?", (DEMO_EMAIL,)).fetchone()
    if demo:
        did = demo["id"]
    else:
        cur = conn.execute(
            "INSERT INTO users(email, password_hash, view_scope, all_display_mode) "
            "VALUES (?, ?, ?, ?)",
            (DEMO_EMAIL, generate_password_hash(DEMO_PASSWORD),
             old.get("view_scope", "single"), old.get("all_display_mode", "value")))
        did = cur.lastrowid

    if no_pf:  # legacy trades with no portfolio -> a portfolio for the demo user
        cur = conn.execute(
            "INSERT INTO portfolios(user_id, name, starting_capital, display_mode) "
            "VALUES (?, '我的组合', 0, 'value')", (did,))
        conn.execute("UPDATE trades SET portfolio_id = ? WHERE portfolio_id IS NULL",
                     (cur.lastrowid,))
    conn.execute("UPDATE portfolios SET user_id = ? WHERE user_id IS NULL", (did,))

    owned = [r["id"] for r in
             conn.execute("SELECT id FROM portfolios WHERE user_id = ? ORDER BY id", (did,))]
    ap = old.get("active_portfolio_id")
    ap = int(ap) if ap and int(ap) in owned else (owned[0] if owned else None)
    conn.execute("UPDATE users SET active_portfolio_id = ? WHERE id = ?", (ap, did))


# ---- users -----------------------------------------------------------------

def create_user(email, password_hash):
    """Create a user with one empty default portfolio set active."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO users(email, password_hash) VALUES (?, ?)",
            (email, password_hash))
        uid = cur.lastrowid
        pcur = conn.execute(
            "INSERT INTO portfolios(user_id, name, starting_capital, display_mode) "
            "VALUES (?, '我的组合', 0, 'value')", (uid,))
        conn.execute("UPDATE users SET active_portfolio_id = ? WHERE id = ?",
                     (pcur.lastrowid, uid))
        conn.commit()
        return uid


def get_user_by_email(email):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return dict(row) if row else None


def get_user_by_id(uid):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return dict(row) if row else None


def set_password(uid, password_hash):
    with get_conn() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, uid))
        conn.commit()


def delete_user(uid):
    """Delete a user and everything they own."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM trades WHERE portfolio_id IN "
            "(SELECT id FROM portfolios WHERE user_id = ?)", (uid,))
        conn.execute("DELETE FROM portfolios WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM email_tokens WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
        conn.commit()


def set_email_verified(uid):
    with get_conn() as conn:
        conn.execute("UPDATE users SET email_verified = 1 WHERE id = ?", (uid,))
        conn.commit()


# ---- email tokens (verify / reset), stored hashed + single-use --------------

def _hash_token(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def create_email_token(user_id, kind, ttl_seconds):
    """Create a single-use token; returns the RAW token to embed in the link."""
    raw = secrets.token_urlsafe(32)
    with get_conn() as conn:
        conn.execute("DELETE FROM email_tokens WHERE user_id = ? AND kind = ?", (user_id, kind))
        conn.execute(
            "INSERT INTO email_tokens(token_hash, user_id, kind, expires_at) VALUES (?, ?, ?, ?)",
            (_hash_token(raw), user_id, kind, int(time.time()) + ttl_seconds))
        conn.commit()
    return raw


def consume_email_token(raw, kind):
    """Validate + burn a token. Returns user_id, or None if invalid/expired/used."""
    th = _hash_token(raw)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM email_tokens WHERE token_hash = ? AND kind = ?", (th, kind)).fetchone()
        if not row or row["used"] or row["expires_at"] < int(time.time()):
            return None
        conn.execute("UPDATE email_tokens SET used = 1 WHERE token_hash = ?", (th,))
        conn.commit()
        return row["user_id"]


# ---- per-user UI state -----------------------------------------------------

def _owned_ids(conn, uid):
    return [r["id"] for r in
            conn.execute("SELECT id FROM portfolios WHERE user_id = ? ORDER BY id", (uid,))]


def get_active_portfolio_id(uid):
    """Return the user's active portfolio id, self-healing if stale."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT active_portfolio_id FROM users WHERE id = ?", (uid,)).fetchone()
        ap = row["active_portfolio_id"] if row else None
        owned = _owned_ids(conn, uid)
        if ap not in owned:
            ap = owned[0] if owned else None
            conn.execute("UPDATE users SET active_portfolio_id = ? WHERE id = ?", (ap, uid))
            conn.commit()
    return ap


def set_active_portfolio_id(uid, pid):
    with get_conn() as conn:
        conn.execute("UPDATE users SET active_portfolio_id = ? WHERE id = ?", (pid, uid))
        conn.commit()


def get_view_scope(uid):
    u = get_user_by_id(uid)
    return (u or {}).get("view_scope", "single")


def set_view_scope(uid, scope):
    with get_conn() as conn:
        conn.execute("UPDATE users SET view_scope = ? WHERE id = ?",
                     (scope if scope in ("single", "all") else "single", uid))
        conn.commit()


def get_all_display_mode(uid):
    u = get_user_by_id(uid)
    return (u or {}).get("all_display_mode", "value")


def set_all_display_mode(uid, mode):
    with get_conn() as conn:
        conn.execute("UPDATE users SET all_display_mode = ? WHERE id = ?", (mode, uid))
        conn.commit()


# ---- portfolios (scoped to a user) -----------------------------------------

def list_portfolios(uid):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT p.*, "
            "(SELECT COUNT(*) FROM trades t WHERE t.portfolio_id = p.id) AS num_trades "
            "FROM portfolios p WHERE p.user_id = ? ORDER BY p.id", (uid,)).fetchall()
    return [dict(r) for r in rows]


def get_portfolio(pid, uid):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM portfolios WHERE id = ? AND user_id = ?",
                           (pid, uid)).fetchone()
    return dict(row) if row else None


def create_portfolio(name, starting_capital, display_mode, uid):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO portfolios(user_id, name, starting_capital, display_mode) "
            "VALUES (?, ?, ?, ?)", (uid, name, float(starting_capital), display_mode))
        conn.commit()
        return cur.lastrowid


def update_portfolio(pid, fields: dict, uid):
    cols, vals = [], []
    for k in ("name", "starting_capital", "display_mode"):
        if k in fields:
            cols.append(f"{k} = ?")
            vals.append(fields[k])
    if not cols:
        return
    vals.extend([pid, uid])
    with get_conn() as conn:
        conn.execute(
            f"UPDATE portfolios SET {', '.join(cols)} WHERE id = ? AND user_id = ?", vals)
        conn.commit()


def delete_portfolio(pid, uid):
    """Delete a portfolio (if owned) and its trades; keep >=1 portfolio per user."""
    with get_conn() as conn:
        owned = conn.execute("SELECT id FROM portfolios WHERE id = ? AND user_id = ?",
                             (pid, uid)).fetchone()
        if owned:
            conn.execute("DELETE FROM trades WHERE portfolio_id = ?", (pid,))
            conn.execute("DELETE FROM portfolios WHERE id = ? AND user_id = ?", (pid, uid))
        remaining = _owned_ids(conn, uid)
        if not remaining:
            cur = conn.execute(
                "INSERT INTO portfolios(user_id, name, starting_capital, display_mode) "
                "VALUES (?, '我的组合', 0, 'value')", (uid,))
            remaining = [cur.lastrowid]
        row = conn.execute(
            "SELECT active_portfolio_id FROM users WHERE id = ?", (uid,)).fetchone()
        if not row or row["active_portfolio_id"] not in remaining:
            conn.execute("UPDATE users SET active_portfolio_id = ? WHERE id = ?",
                         (remaining[0], uid))
        conn.commit()
        return remaining[0]


# ---- trades ----------------------------------------------------------------

def list_trades(portfolio_id):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE portfolio_id = ? ORDER BY date ASC, id ASC",
            (portfolio_id,)).fetchall()
    return [dict(r) for r in rows]


def list_all_trades(uid):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT t.*, p.name AS portfolio_name FROM trades t "
            "JOIN portfolios p ON p.id = t.portfolio_id "
            "WHERE p.user_id = ? ORDER BY t.date ASC, t.id ASC", (uid,)).fetchall()
    return [dict(r) for r in rows]


def add_trade(t: dict, portfolio_id: int):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO trades(portfolio_id, date, ticker, side, shares, price, fees, reason) "
            "VALUES (:pid, :date, :ticker, :side, :shares, :price, :fees, :reason)",
            {**t, "pid": portfolio_id})
        conn.commit()
        return cur.lastrowid


def update_trade(trade_id: int, t: dict, uid: int):
    """Update only if the trade belongs to one of the user's portfolios."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE trades SET date=:date, ticker=:ticker, side=:side, shares=:shares, "
            "price=:price, fees=:fees, reason=:reason WHERE id=:id AND portfolio_id IN "
            "(SELECT id FROM portfolios WHERE user_id=:uid)",
            {**t, "id": trade_id, "uid": uid})
        conn.commit()
        return cur.rowcount


def delete_trade(trade_id: int, uid: int):
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM trades WHERE id = ? AND portfolio_id IN "
            "(SELECT id FROM portfolios WHERE user_id = ?)", (trade_id, uid))
        conn.commit()
        return cur.rowcount


def clear_trades(portfolio_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM trades WHERE portfolio_id = ?", (portfolio_id,))
        conn.commit()


# ---- price cache (shared across users) -------------------------------------

def get_cached_prices(ticker: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT date, close FROM price_cache WHERE ticker = ? ORDER BY date",
            (ticker,)).fetchall()
    return {r["date"]: r["close"] for r in rows}


def store_prices(ticker: str, series: dict, source: str):
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO price_cache(ticker, date, close) VALUES (?, ?, ?) "
            "ON CONFLICT(ticker, date) DO UPDATE SET close = excluded.close",
            [(ticker, d, c) for d, c in series.items()])
        conn.execute(
            "INSERT INTO price_meta(ticker, last_fetched, source) "
            "VALUES (?, datetime('now'), ?) "
            "ON CONFLICT(ticker) DO UPDATE SET last_fetched=datetime('now'), source=excluded.source",
            (ticker, source))
        conn.commit()


def get_price_meta():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM price_meta").fetchall()
    return {r["ticker"]: dict(r) for r in rows}


def clear_price_cache(ticker: str | None = None):
    with get_conn() as conn:
        if ticker:
            conn.execute("DELETE FROM price_cache WHERE ticker = ?", (ticker,))
            conn.execute("DELETE FROM price_meta WHERE ticker = ?", (ticker,))
        else:
            conn.execute("DELETE FROM price_cache")
            conn.execute("DELETE FROM price_meta")
        conn.commit()
