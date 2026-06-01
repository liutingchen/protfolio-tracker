"""Historical daily close prices with on-disk caching and several fallbacks.

Provider order (first that returns data wins):
  1. yfinance                       (no key)
  2. Yahoo chart API direct + crumb (no key)
  3. Stooq CSV                       (optional key: env STOOQ_API_KEY)
  4. Alpha Vantage                   (optional key: env ALPHAVANTAGE_API_KEY)

Free public sources (Yahoo) are occasionally rate-limited from data-center
IPs. If that happens, set ALPHAVANTAGE_API_KEY (free at alphavantage.co) or
STOOQ_API_KEY and it will be used automatically.
"""
import datetime as dt
import io
import os
import threading
import time

import pandas as pd
import requests

import db

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# Serialize outbound price fetches across all gunicorn threads. With 8 threads
# each fetching a batch of tickers, hitting the source concurrently trips its
# rate limiting (empty/429 responses). A global lock + small spacing makes the
# requests queue politely instead, which is reliable and still fast enough.
_FETCH_LOCK = threading.Lock()
_MIN_SPACING_S = 0.15
_last_fetch_at = [0.0]


# --------------------------------------------------------------------------- #
#  Providers — each returns {date_str: close_float} or None
# --------------------------------------------------------------------------- #
_SA_SESSION = requests.Session()
_SA_SESSION.headers.update({"User-Agent": UA, "Accept": "application/json"})


def _fetch_stockanalysis(ticker, start, end):
    """stockanalysis.com — keyless daily OHLCV. Robust default.

    Uses the `period=Daily` endpoint, which returns full bars
    {t,o,h,l,c,v,a} (a = split/div-adjusted close) up to the latest trading
    day. Returns {date: {open,high,low,close,volume}}.

    Retries transient failures (429/5xx/empty) with backoff, since a burst of
    cold fetches can briefly rate-limit us.
    """
    for kind in ("s", "e"):  # 's' = stock, 'e' = ETF
        url = (f"https://stockanalysis.com/api/symbol/{kind}/{ticker}"
               f"/history?range=10Y&period=Daily")
        data = None
        for attempt in range(3):
            try:
                r = _SA_SESSION.get(url, timeout=20)
                if r.status_code == 200:
                    j = r.json()
                    data = j.get("data")
                    if isinstance(data, list) and data and isinstance(data[0], dict):
                        break
                    data = None  # empty/unexpected -> retry
                elif r.status_code in (429, 500, 502, 503, 504):
                    pass         # transient -> retry
                else:
                    break        # 404 etc. -> try the other kind
            except Exception:
                pass
            time.sleep(0.6 * (attempt + 1))   # 0.6s, 1.2s backoff
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            continue
        out = {}
        for row in data:
            d = str(row.get("t"))
            c = row.get("c")
            if not d or c is None or not (start <= d <= end):
                continue
            try:
                out[d] = {
                    "close": float(c),
                    "open": float(row["o"]) if row.get("o") is not None else None,
                    "high": float(row["h"]) if row.get("h") is not None else None,
                    "low": float(row["l"]) if row.get("l") is not None else None,
                    "volume": float(row["v"]) if row.get("v") is not None else None,
                }
            except (TypeError, ValueError):
                continue
        if out:
            return out
    return None


def _fetch_yfinance(ticker, start, end):
    import yfinance as yf

    end_excl = (dt.date.fromisoformat(end) + dt.timedelta(days=1)).isoformat()
    for attempt in range(2):
        try:
            df = yf.download(ticker, start=start, end=end_excl, auto_adjust=True,
                             progress=False, threads=False)
        except Exception:
            df = None
        if df is not None and not df.empty:
            close = df["Close"]
            if isinstance(close, pd.DataFrame):
                close = close.iloc[:, 0]
            close = close.dropna()
            if not close.empty:
                return {i.strftime("%Y-%m-%d"): float(v) for i, v in close.items()}
        time.sleep(1.0)
    return None


def _yahoo_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    crumb = None
    try:
        s.get("https://fc.yahoo.com", timeout=10)
        r = s.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=10)
        if r.status_code == 200 and r.text and "<" not in r.text:
            crumb = r.text.strip()
    except Exception:
        pass
    return s, crumb


def _fetch_yahoo_direct(ticker, start, end):
    p1 = int(dt.datetime.fromisoformat(start + "T00:00:00").timestamp())
    p2 = int(dt.datetime.fromisoformat(end + "T00:00:00").timestamp()) + 172800
    s, crumb = _yahoo_session()
    base = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"period1": p1, "period2": p2, "interval": "1d",
              "events": "div,split"}
    if crumb:
        params["crumb"] = crumb
    for host in ("query1", "query2"):
        url = base.replace("query1", host)
        try:
            r = s.get(url, params=params, timeout=20)
            if r.status_code != 200:
                time.sleep(0.5)
                continue
            data = r.json()
            res = (data.get("chart", {}).get("result") or [None])[0]
            if not res:
                continue
            ts = res.get("timestamp") or []
            ind = res.get("indicators", {})
            closes = (ind.get("quote") or [{}])[0].get("close") or []
            adj = (ind.get("adjclose") or [{}])[0].get("adjclose")
            vals = adj if adj else closes
            out = {}
            for t, v in zip(ts, vals):
                if v is not None:
                    out[dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d")] = float(v)
            if out:
                return out
        except Exception:
            continue
    return None


def _fetch_stooq(ticker, start, end):
    sym = ticker.lower()
    if "." not in sym:
        sym += ".us"
    url = (f"https://stooq.com/q/d/l/?s={sym}&i=d"
           f"&d1={start.replace('-', '')}&d2={end.replace('-', '')}")
    key = os.environ.get("STOOQ_API_KEY")
    if key:
        url += f"&apikey={key}"
    try:
        r = requests.get(url, timeout=25, headers={"User-Agent": UA})
    except Exception:
        return None
    if r.status_code != 200 or "Date" not in r.text[:200]:
        return None
    df = pd.read_csv(io.StringIO(r.text))
    if df.empty or "Close" not in df.columns:
        return None
    return {str(row["Date"]): float(row["Close"])
            for _, row in df.iterrows() if pd.notna(row["Close"])} or None


def _fetch_alphavantage(ticker, start, end):
    key = os.environ.get("ALPHAVANTAGE_API_KEY")
    if not key:
        return None
    url = ("https://www.alphavantage.co/query?function=TIME_SERIES_DAILY_ADJUSTED"
           f"&symbol={ticker}&outputsize=full&apikey={key}")
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": UA})
        j = r.json()
    except Exception:
        return None
    series = j.get("Time Series (Daily)")
    if not series:
        return None
    out = {}
    for d, row in series.items():
        if start <= d <= end:
            px = row.get("5. adjusted close") or row.get("4. close")
            if px:
                out[d] = float(px)
    return out or None


def _fetch_stooq_quote(ticker, start, end):
    """Stooq's keyless quote CSV — latest close only (one data point).

    The full-history CSV now needs an API key, but this lightweight quote
    endpoint stays keyless. Good as a last-resort so a holding at least shows a
    current price even if the history source is down.
    """
    sym = ticker.lower()
    if "." not in sym:
        sym += ".us"
    url = f"https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv"
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": UA})
    except Exception:
        return None
    if r.status_code != 200 or "Close" not in r.text[:60]:
        return None
    try:
        df = pd.read_csv(io.StringIO(r.text))
        row = df.iloc[0]
        d, px = str(row["Date"]), float(row["Close"])
    except Exception:
        return None
    if d in ("N/D", "") or px <= 0 or not (start <= d <= end):
        return None
    return {d: px}


# Yahoo/yfinance are intentionally NOT used: they are reliably rate-limited from
# data-center IPs (Railway) and only added latency + noise. stockanalysis is the
# primary (full history); stooq-quote is a keyless latest-price fallback;
# Alpha Vantage is used only if ALPHAVANTAGE_API_KEY is set.
PROVIDERS = [
    ("stockanalysis", _fetch_stockanalysis),
    ("alphavantage", _fetch_alphavantage),
    ("stooq_quote", _fetch_stooq_quote),
]


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _fetch_quote(ticker):
    """Real-time (intraday) quote for one ticker. Returns (bar, asof_date) or
    (None, None), where bar = {close, open, high, low, volume} for the current
    session (`p` = live price, `o/h/l/v` = today's intraday range/volume)."""
    for kind in ("s", "e"):
        url = f"https://stockanalysis.com/api/quotes/{kind}/{ticker}"
        try:
            with _FETCH_LOCK:
                gap = _MIN_SPACING_S - (time.monotonic() - _last_fetch_at[0])
                if gap > 0:
                    time.sleep(gap)
                try:
                    r = _SA_SESSION.get(url, timeout=12)
                finally:
                    _last_fetch_at[0] = time.monotonic()
            if r.status_code != 200:
                continue
            d = (r.json() or {}).get("data") or {}
            px = _f(d.get("p"))
            if px is None or px <= 0:
                continue
            bar = {"close": px, "open": _f(d.get("o")), "high": _f(d.get("h")),
                   "low": _f(d.get("l")), "volume": _f(d.get("v"))}
            ts = d.get("ts")
            asof = (dt.datetime.utcfromtimestamp(float(ts) / 1000).strftime("%Y-%m-%d")
                    if ts else None)
            return bar, asof
        except Exception:
            continue
    return None, None


def get_quotes(tickers, today):
    """Fetch live intraday bars (price + today's O/H/L/V) and upsert each into
    the cache dated `today` so the valuation and weekly chart pick them up as
    the latest, complete bar. Returns {ticker: price}."""
    out = {}
    for t in tickers:
        t = t.upper()
        bar, asof = _fetch_quote(t)
        if bar is not None:
            day = max(today, asof) if asof else today
            db.store_prices(t, {day: bar}, "stockanalysis-quote")
            out[t] = bar["close"]
    return out


# --------------------------------------------------------------------------- #
#  Cache + public API
# --------------------------------------------------------------------------- #
def _needs_refetch(cached, start, end):
    if not cached:
        return True
    cmin, cmax = min(cached), max(cached)
    if cmin > start:
        return True
    # Refetch when the cache falls behind the last *completed* trading day, so
    # prices advance ~daily on their own. Target = the last weekday strictly
    # before today; this gives one full day of grace so a normal page load
    # doesn't refetch repeatedly while today's close hasn't been published yet
    # (which would hammer the source). Use 刷新股价 for the immediate latest.
    target = dt.date.fromisoformat(end) - dt.timedelta(days=1)
    while target.weekday() >= 5:                # Sat/Sun -> back to Friday
        target -= dt.timedelta(days=1)
    if dt.date.fromisoformat(cmax) < target:
        return True
    return False


# Wall-clock budget (seconds) for fetching within a single chart request, so a
# slow/blocked source can never hang the page. Once exceeded, remaining tickers
# are served from cache (even if stale); 刷新股价 / a later load fills the rest.
# 0 disables fetching entirely (cache-only). Override via PRICE_FETCH_BUDGET.
FETCH_BUDGET_S = float(os.environ.get("PRICE_FETCH_BUDGET", "12"))


def _fetch_one(ticker, start, end):
    # One outbound fetch at a time across all threads, with light spacing, so
    # concurrent chart requests don't burst the source into rate-limiting.
    with _FETCH_LOCK:
        gap = _MIN_SPACING_S - (time.monotonic() - _last_fetch_at[0])
        if gap > 0:
            time.sleep(gap)
        try:
            for name, fn in PROVIDERS:
                try:
                    fetched = fn(ticker, start, end)
                except Exception:
                    fetched = None
                if fetched:
                    return fetched, name
            return None, None
        finally:
            _last_fetch_at[0] = time.monotonic()


def get_daily_closes(tickers, start, end, force=False, budget=None):
    """Return ({ticker: pd.Series indexed by Timestamp}, {ticker: error_msg}).

    Fetching is bounded by `budget` seconds (default FETCH_BUDGET_S): tickers
    with NO cache are fetched first (they'd otherwise be blank), then merely-
    stale ones, until the budget runs out. Whatever is cached (fresh or stale)
    is always returned so the chart renders instead of hanging.
    """
    if budget is None:
        budget = FETCH_BUDGET_S
    result, errors = {}, {}
    cache = {t.upper(): db.get_cached_prices(t.upper()) for t in tickers}

    # decide what needs fetching, prioritizing empty caches over stale ones
    need_empty, need_stale = [], []
    for t, cached in cache.items():
        if force or not cached:
            (need_empty if not cached else need_stale).append(t)
        elif _needs_refetch(cached, start, end):
            need_stale.append(t)

    deadline = time.monotonic() + budget
    for t in need_empty + need_stale:
        if budget <= 0 or time.monotonic() >= deadline:
            break
        fetched, source = _fetch_one(t, start, end)
        if fetched:
            db.store_prices(t, fetched, source)
            cache[t] = db.get_cached_prices(t)

    for t, cached in cache.items():
        if cached:
            s = pd.Series(cached, dtype="float64")
            s.index = pd.to_datetime(s.index)
            result[t] = s.sort_index()
        else:
            errors[t] = ("No price data yet (source slow/unavailable); "
                         "try 刷新股价 again in a moment.")
    return result, errors
