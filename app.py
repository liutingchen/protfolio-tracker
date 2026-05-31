"""Portfolio Tracker — Flask backend (multi-user).

Run:  python app.py     then open  http://127.0.0.1:5174
"""
import datetime as dt
import os
import re
import secrets
import time
from collections import defaultdict
from functools import wraps

from flask import Flask, jsonify, redirect, request, session
from werkzeug.security import check_password_hash, generate_password_hash

import db
import mailer
import portfolio

app = Flask(__name__, static_folder="static", static_url_path="")

VALID_MODES = ("pnl", "value", "index")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# --------------------------------------------------------------------------- #
#  App / session setup
# --------------------------------------------------------------------------- #
def _load_secret_key():
    env = os.environ.get("SECRET_KEY")  # preferred in production (e.g. Render)
    if env:
        return env
    path = os.path.join(db.DATA_DIR, "secret_key")
    os.makedirs(db.DATA_DIR, exist_ok=True)
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    key = os.urandom(32).hex()
    with open(path, "w") as f:
        f.write(key)
    return key


app.secret_key = _load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=dt.timedelta(days=30),
)

# Production hardening: HTTPS-only cookies + trust the reverse proxy.
IS_PROD = os.environ.get("PORTFOLIO_ENV", "").lower() in ("prod", "production")
if IS_PROD:
    app.config["SESSION_COOKIE_SECURE"] = True
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

db.init_db()  # ensure schema/migration runs under gunicorn too (use --preload)


# --------------------------------------------------------------------------- #
#  CSRF (double-submit token) + login rate limiting
# --------------------------------------------------------------------------- #
@app.before_request
def _csrf_protect():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    if request.method in ("POST", "PUT", "DELETE", "PATCH") and request.path.startswith("/api/"):
        if request.headers.get("X-CSRF-Token", "") != session.get("csrf"):
            return jsonify({"error": "CSRF 校验失败，请刷新页面后重试。"}), 403


@app.after_request
def _set_csrf_cookie(resp):
    token = session.get("csrf")
    if token:
        resp.set_cookie("csrftoken", token, samesite="Lax",
                        secure=app.config.get("SESSION_COOKIE_SECURE", False),
                        httponly=False)
    return resp


_attempts = defaultdict(list)  # per-process per-IP login/register attempts


def _rate_limited(key, limit, window):
    now = time.time()
    arr = _attempts[key]
    while arr and arr[0] < now - window:
        arr.pop(0)
    if len(arr) >= limit:
        return True
    arr.append(now)
    return False


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("uid"):
            return jsonify({"error": "未登录，请先登录。"}), 401
        return f(*args, **kwargs)
    return wrapper


def _uid():
    return session.get("uid")


# --------------------------------------------------------------------------- #
#  Validation helpers
# --------------------------------------------------------------------------- #
def _parse_trade(data):
    try:
        date = str(data["date"]).strip()
        dt.date.fromisoformat(date)
        ticker = str(data["ticker"]).strip().upper()
        side = str(data["side"]).strip().lower()
        shares = float(data["shares"])
        price = float(data["price"])
        fees = float(data.get("fees", 0) or 0)
        reason = str(data.get("reason", "") or "").strip()
    except (KeyError, ValueError, TypeError):
        return None, "Invalid or missing fields."
    if not ticker:
        return None, "Ticker is required."
    if side not in ("buy", "sell"):
        return None, "Side must be 'buy' or 'sell'."
    if shares <= 0:
        return None, "Shares must be greater than 0."
    if price < 0 or fees < 0:
        return None, "Price and fees cannot be negative."
    return {"date": date, "ticker": ticker, "side": side, "shares": shares,
            "price": price, "fees": fees, "reason": reason}, None


def _parse_portfolio_fields(data, require_name=False):
    out = {}
    if "name" in data or require_name:
        name = str(data.get("name", "")).strip()
        if not name:
            return None, "Portfolio name is required."
        if len(name) > 60:
            return None, "Name too long (max 60 chars)."
        out["name"] = name
    if "starting_capital" in data:
        try:
            out["starting_capital"] = float(data["starting_capital"] or 0)
        except (ValueError, TypeError):
            return None, "starting_capital must be a number."
    if "display_mode" in data:
        if data["display_mode"] not in VALID_MODES:
            return None, "invalid display_mode."
        out["display_mode"] = data["display_mode"]
    return out, None


def _scope():
    return db.get_view_scope(_uid())


# --------------------------------------------------------------------------- #
#  Pages
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return app.send_static_file("index.html")


# --------------------------------------------------------------------------- #
#  Auth
# --------------------------------------------------------------------------- #
def _public_user(u):
    return {"id": u["id"], "email": u["email"]}


def _base_url():
    env = os.environ.get("APP_BASE_URL")
    return env.rstrip("/") if env else request.url_root.rstrip("/")


def _send_verification_email(uid, email):
    token = db.create_email_token(uid, "verify", 24 * 3600)
    link = f"{_base_url()}/verify?token={token}"
    try:
        mailer.send_verification(email, link)
    except Exception as e:
        app.logger.warning("verification email failed: %s", e)


@app.post("/api/auth/register")
def register():
    if _rate_limited(f"auth:{request.remote_addr or '?'}", 15, 300):
        return jsonify({"error": "尝试过于频繁，请 5 分钟后再试。"}), 429
    data = request.get_json(force=True, silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    if not EMAIL_RE.match(email):
        return jsonify({"error": "请输入有效的邮箱地址。"}), 400
    if len(password) < 6:
        return jsonify({"error": "密码至少需要 6 位。"}), 400
    if db.get_user_by_email(email):
        return jsonify({"error": "该邮箱已注册，请直接登录。"}), 409
    uid = db.create_user(email, generate_password_hash(password))
    _send_verification_email(uid, email)
    return jsonify({
        "ok": True, "needs_verification": True,
        "message": "验证邮件已发送，请查收并点击链接完成验证后再登录。",
        "dev": not mailer.email_configured(),
    }), 201


@app.post("/api/auth/login")
def login():
    if _rate_limited(f"auth:{request.remote_addr or '?'}", 15, 300):
        return jsonify({"error": "尝试过于频繁，请 5 分钟后再试。"}), 429
    data = request.get_json(force=True, silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    u = db.get_user_by_email(email)
    if not u or not check_password_hash(u["password_hash"], password):
        return jsonify({"error": "邮箱或密码不正确。"}), 401
    if not u["email_verified"]:
        return jsonify({"error": "邮箱尚未验证，请查收验证邮件后再登录。",
                        "code": "unverified", "email": email}), 403
    session.clear()
    session["uid"] = u["id"]
    session["csrf"] = secrets.token_hex(16)  # rotate token on auth
    session.permanent = True
    return jsonify(_public_user(u))


@app.post("/api/auth/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/auth/me")
def me():
    uid = _uid()
    u = db.get_user_by_id(uid) if uid else None
    if not u:
        session.pop("uid", None)  # keep csrf token alive for the login POST
        return jsonify({"error": "未登录"}), 401
    return jsonify(_public_user(u))


@app.get("/verify")
def verify_email():
    uid = db.consume_email_token(request.args.get("token", ""), "verify")
    if uid:
        db.set_email_verified(uid)
        return redirect("/?verified=1")
    return redirect("/?verify_error=1")


@app.get("/reset")
def reset_page():
    return app.send_static_file("index.html")  # SPA reads ?token= and shows the form


@app.post("/api/auth/resend-verification")
def resend_verification():
    if _rate_limited(f"auth:{request.remote_addr or '?'}", 15, 300):
        return jsonify({"error": "尝试过于频繁，请稍后再试。"}), 429
    email = str((request.get_json(force=True, silent=True) or {}).get("email", "")).strip().lower()
    u = db.get_user_by_email(email)
    if u and not u["email_verified"]:
        _send_verification_email(u["id"], email)
    return jsonify({"ok": True, "message": "如果该邮箱待验证，验证邮件已重新发送。"})


@app.post("/api/auth/forgot-password")
def forgot_password():
    if _rate_limited(f"auth:{request.remote_addr or '?'}", 15, 300):
        return jsonify({"error": "尝试过于频繁，请稍后再试。"}), 429
    email = str((request.get_json(force=True, silent=True) or {}).get("email", "")).strip().lower()
    u = db.get_user_by_email(email)
    if u:
        token = db.create_email_token(u["id"], "reset", 3600)
        link = f"{_base_url()}/reset?token={token}"
        try:
            mailer.send_password_reset(email, link)
        except Exception as e:
            app.logger.warning("reset email failed: %s", e)
    return jsonify({"ok": True, "message": "如果该邮箱已注册，重置链接已发送到邮箱。"})


@app.post("/api/auth/reset-password")
def reset_password():
    data = request.get_json(force=True, silent=True) or {}
    token = str(data.get("token", ""))
    new = str(data.get("new_password", ""))
    if len(new) < 6:
        return jsonify({"error": "新密码至少需要 6 位。"}), 400
    uid = db.consume_email_token(token, "reset")
    if not uid:
        return jsonify({"error": "链接无效或已过期，请重新申请。"}), 400
    db.set_password(uid, generate_password_hash(new))
    db.set_email_verified(uid)  # a successful reset also proves email ownership
    return jsonify({"ok": True, "message": "密码已重置，请用新密码登录。"})


@app.post("/api/auth/change-password")
@login_required
def change_password():
    data = request.get_json(force=True, silent=True) or {}
    current = str(data.get("current_password", ""))
    new = str(data.get("new_password", ""))
    u = db.get_user_by_id(_uid())
    if not u or not check_password_hash(u["password_hash"], current):
        return jsonify({"error": "当前密码不正确。"}), 400
    if len(new) < 6:
        return jsonify({"error": "新密码至少需要 6 位。"}), 400
    db.set_password(_uid(), generate_password_hash(new))
    return jsonify({"ok": True})


@app.post("/api/auth/delete-account")
@login_required
def delete_account():
    data = request.get_json(force=True, silent=True) or {}
    password = str(data.get("password", ""))
    u = db.get_user_by_id(_uid())
    if not u or not check_password_hash(u["password_hash"], password):
        return jsonify({"error": "密码不正确，删除未执行。"}), 400
    db.delete_user(_uid())
    session.clear()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
#  Portfolios
# --------------------------------------------------------------------------- #
@app.get("/api/portfolios")
@login_required
def get_portfolios():
    return jsonify({"portfolios": db.list_portfolios(_uid()),
                    "active_id": db.get_active_portfolio_id(_uid())})


@app.post("/api/portfolios")
@login_required
def create_portfolio():
    fields, err = _parse_portfolio_fields(request.get_json(force=True, silent=True) or {},
                                          require_name=True)
    if err:
        return jsonify({"error": err}), 400
    pid = db.create_portfolio(fields["name"], fields.get("starting_capital", 0.0),
                              fields.get("display_mode", "value"), _uid())
    db.set_active_portfolio_id(_uid(), pid)
    db.set_view_scope(_uid(), "single")
    return jsonify({"id": pid, "active_id": pid}), 201


@app.put("/api/portfolios/<int:pid>")
@login_required
def edit_portfolio(pid):
    fields, err = _parse_portfolio_fields(request.get_json(force=True, silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    if not db.get_portfolio(pid, _uid()):
        return jsonify({"error": "Portfolio not found."}), 404
    db.update_portfolio(pid, fields, _uid())
    return jsonify({"ok": True})


@app.delete("/api/portfolios/<int:pid>")
@login_required
def remove_portfolio(pid):
    if not db.get_portfolio(pid, _uid()):
        return jsonify({"error": "Portfolio not found."}), 404
    new_active = db.delete_portfolio(pid, _uid())
    return jsonify({"ok": True, "active_id": new_active})


@app.post("/api/portfolios/<int:pid>/activate")
@login_required
def activate_portfolio(pid):
    if not db.get_portfolio(pid, _uid()):
        return jsonify({"error": "Portfolio not found."}), 404
    db.set_active_portfolio_id(_uid(), pid)
    db.set_view_scope(_uid(), "single")
    return jsonify({"ok": True, "active_id": pid})


@app.post("/api/view-all")
@login_required
def activate_all():
    db.set_view_scope(_uid(), "all")
    return jsonify({"ok": True, "active_id": "all"})


# --------------------------------------------------------------------------- #
#  State + chart
# --------------------------------------------------------------------------- #
@app.get("/api/state")
@login_required
def get_state():
    uid = _uid()
    portfolios = db.list_portfolios(uid)
    if _scope() == "all":
        cap = sum(float(p["starting_capital"] or 0) for p in portfolios)
        return jsonify({
            "trades": db.list_all_trades(uid),
            "portfolio": {"id": "all", "name": "全部组合", "count": len(portfolios)},
            "settings": {"starting_capital": cap,
                         "display_mode": db.get_all_display_mode(uid)},
            "portfolios": portfolios, "active_id": "all",
            "price_meta": db.get_price_meta(),
        })
    active = db.get_active_portfolio_id(uid)
    p = db.get_portfolio(active, uid)
    return jsonify({
        "trades": db.list_trades(active) if p else [],
        "portfolio": p,
        "settings": {"starting_capital": p["starting_capital"] if p else 0,
                     "display_mode": p["display_mode"] if p else "value"},
        "portfolios": portfolios, "active_id": active,
        "price_meta": db.get_price_meta(),
    })


@app.get("/api/chart")
@login_required
def get_chart():
    uid = _uid()
    return jsonify(portfolio.compute_all(uid) if _scope() == "all"
                   else portfolio.compute(db.get_active_portfolio_id(uid), uid))


# --------------------------------------------------------------------------- #
#  Trades
# --------------------------------------------------------------------------- #
@app.post("/api/trades")
@login_required
def create_trade():
    if _scope() == "all":
        return jsonify({"error": "合并视图下不能添加交易，请切换到具体组合。"}), 400
    trade, err = _parse_trade(request.get_json(force=True, silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    active = db.get_active_portfolio_id(_uid())
    if not db.get_portfolio(active, _uid()):
        return jsonify({"error": "No active portfolio."}), 400
    tid = db.add_trade(trade, active)
    return jsonify({"id": tid, "trade": trade}), 201


@app.put("/api/trades/<int:trade_id>")
@login_required
def edit_trade(trade_id):
    trade, err = _parse_trade(request.get_json(force=True, silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    if db.update_trade(trade_id, trade, _uid()) == 0:
        return jsonify({"error": "Trade not found."}), 404
    return jsonify({"ok": True})


@app.delete("/api/trades/<int:trade_id>")
@login_required
def remove_trade(trade_id):
    if db.delete_trade(trade_id, _uid()) == 0:
        return jsonify({"error": "Trade not found."}), 404
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
#  Settings, prices, seed, clear
# --------------------------------------------------------------------------- #
@app.post("/api/settings")
@login_required
def update_settings():
    fields, err = _parse_portfolio_fields(request.get_json(force=True, silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    if _scope() == "all":
        if "display_mode" in fields:
            db.set_all_display_mode(_uid(), fields["display_mode"])
        return jsonify({"ok": True})
    if fields:
        db.update_portfolio(db.get_active_portfolio_id(_uid()), fields, _uid())
    return jsonify({"ok": True})


@app.post("/api/refresh-prices")
@login_required
def refresh_prices():
    data = request.get_json(force=True, silent=True) or {}
    db.clear_price_cache(data.get("ticker"))
    return jsonify({"ok": True})


@app.post("/api/clear")
@login_required
def clear_all():
    if _scope() == "all":
        return jsonify({"error": "合并视图下不能清空，请先切换到具体组合。"}), 400
    db.clear_trades(db.get_active_portfolio_id(_uid()))
    return jsonify({"ok": True})


SEED_TRADES = [
    ("2024-02-12", "NVDA", "buy", 60, 72.0, "突破平底盘整，放量过买点，AI 龙头"),
    ("2024-03-01", "MSFT", "buy", 40, 410.0, "带柄茶杯突破，云业务+Copilot 催化"),
    ("2024-04-22", "AAPL", "buy", 80, 165.0, "回踩 10 周线企稳，蓄势待发"),
    ("2024-06-10", "NVDA", "buy", 40, 121.0, "拆股后顺势加仓，相对强度新高"),
    ("2024-07-17", "AAPL", "sell", 40, 224.0, "冲高放量，先减半锁定利润"),
    ("2024-08-05", "AMZN", "buy", 50, 162.0, "大盘恐慌回调，逢低布局电商+云"),
    ("2024-10-31", "MSFT", "sell", 40, 406.0, "财报后跌破 10 周线，止盈离场"),
    ("2024-11-18", "NVDA", "sell", 50, 140.0, "见顶背离，兑现部分利润"),
    ("2025-01-27", "NVDA", "buy", 50, 118.0, "DeepSeek 恐慌错杀，趋势未破加回"),
    ("2025-03-10", "AAPL", "sell", 40, 227.0, "关税扰动，清掉剩余仓位观望"),
    ("2025-05-13", "AMZN", "buy", 30, 208.0, "突破下降趋势线，重回 40 周线上方"),
    ("2025-09-09", "NVDA", "buy", 30, 170.0, "新一轮底部突破，AI 资本开支再加速"),
]


@app.post("/api/seed")
@login_required
def seed():
    if _scope() == "all":
        return jsonify({"error": "请先切换到具体组合再加载示例数据。"}), 400
    active = db.get_active_portfolio_id(_uid())
    if db.list_trades(active):
        return jsonify({"error": "This portfolio already has trades; clear them first."}), 400
    db.update_portfolio(active, {"starting_capital": 50000, "display_mode": "value"}, _uid())
    for date, ticker, side, shares, price, reason in SEED_TRADES:
        db.add_trade({"date": date, "ticker": ticker, "side": side,
                      "shares": float(shares), "price": float(price),
                      "fees": 0.0, "reason": reason}, active)
    return jsonify({"ok": True, "count": len(SEED_TRADES)})


if __name__ == "__main__":
    db.init_db()
    port = int(os.environ.get("PORT", "5174"))
    print(f"Portfolio Tracker running at  http://127.0.0.1:{port}")
    print(f"Demo login: {db.DEMO_EMAIL} / {db.DEMO_PASSWORD}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
