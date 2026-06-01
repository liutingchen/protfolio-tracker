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
import time

import pandas as pd
import requests

import db

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")


# --------------------------------------------------------------------------- #
#  Providers — each returns {date_str: close_float} or None
# --------------------------------------------------------------------------- #
def _fetch_stockanalysis(ticker, start, end):
    """stockanalysis.com — keyless daily closes. Robust default.

    Uses the `type=chart` endpoint: array rows [epoch_ms, close] covering full
    history up to the latest trading day. (The older `period=Daily` form now
    returns ~10-year-stale data, so we avoid it.)
    """
    for kind in ("s", "e"):  # 's' = stock, 'e' = ETF
        url = (f"https://stockanalysis.com/api/symbol/{kind}/{ticker}"
               f"/history?type=chart&range=10Y")
        try:
            r = requests.get(url, timeout=25, headers={"User-Agent": UA})
            if r.status_code != 200:
                continue
            j = r.json()
        except Exception:
            continue
        data = j.get("data")
        if not isinstance(data, list) or not data:
            continue
        out = {}
        for row in data:
            try:
                if isinstance(row, dict):          # {"t": "YYYY-MM-DD", "c": ...}
                    d, px = str(row.get("t")), row.get("a", row.get("c"))
                else:                              # [epoch_ms, close]
                    d = dt.datetime.utcfromtimestamp(row[0] / 1000).strftime("%Y-%m-%d")
                    px = row[1]
            except (TypeError, IndexError, ValueError):
                continue
            if d and px is not None and start <= d <= end:
                out[d] = float(px)
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


PROVIDERS = [
    ("stockanalysis", _fetch_stockanalysis),
    ("yfinance", _fetch_yfinance),
    ("yahoo", _fetch_yahoo_direct),
    ("stooq", _fetch_stooq),
    ("alphavantage", _fetch_alphavantage),
]


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


def get_daily_closes(tickers, start, end, force=False):
    """Return ({ticker: pd.Series indexed by Timestamp}, {ticker: error_msg})."""
    result, errors = {}, {}
    for t in tickers:
        t = t.upper()
        cached = db.get_cached_prices(t)
        if force or _needs_refetch(cached, start, end):
            fetched, source = None, None
            for name, fn in PROVIDERS:
                try:
                    fetched = fn(t, start, end)
                except Exception:
                    fetched = None
                if fetched:
                    source = name
                    break
            if fetched:
                db.store_prices(t, fetched, source)
                cached = db.get_cached_prices(t)

        if cached:
            s = pd.Series(cached, dtype="float64")
            s.index = pd.to_datetime(s.index)
            result[t] = s.sort_index()
        else:
            errors[t] = ("No price data (Yahoo may be rate-limited; "
                         "set ALPHAVANTAGE_API_KEY for a reliable fallback).")
    return result, errors
