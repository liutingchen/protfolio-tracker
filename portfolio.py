"""Turn a trade log into a MarketSurge-style portfolio chart series.

Pipeline:
  trades -> daily holdings (shares per ticker) -> daily mark-to-market value
         -> weekly OHLC candles + 10-week MA + volume + trade markers
"""
import datetime as dt
import math

import pandas as pd

import db
import prices


def _round(x, n=2):
    if x is None:
        return None
    try:
        if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
            return None
    except TypeError:
        return None
    return round(float(x), n)


def _series_to_points(s: pd.Series, n=2):
    """pandas Series -> [{time, value}] dropping NaN, sorted by time."""
    out = []
    for ts, v in s.items():
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        out.append({"time": ts.strftime("%Y-%m-%d"), "value": _round(v, n)})
    return out


def _align(ts: pd.Timestamp, index: pd.DatetimeIndex):
    """Snap a trade date onto an existing bar time (first bar >= ts)."""
    if len(index) == 0:
        return ts
    pos = int(index.searchsorted(ts, side="left"))
    if pos >= len(index):
        pos = len(index) - 1
    return index[pos]


# Moving-average sets per timeframe (MarketSurge style).
#   weekly: 10- & 40-week SMA
#   daily:  10/21 EMA + 50/150/200 SMA
_MA_SETS = {
    "weekly": [
        {"key": "sma10", "label": "SMA(10)", "color": "#2962ff", "kind": "sma", "n": 10},
        {"key": "sma40", "label": "SMA(40)", "color": "#ff9800", "kind": "sma", "n": 40},
    ],
    "daily": [
        {"key": "ema10", "label": "EMA(10)",  "color": "#2962ff", "kind": "ema", "n": 10},
        {"key": "ema21", "label": "EMA(21)",  "color": "#e91e63", "kind": "ema", "n": 21},
        {"key": "sma50", "label": "SMA(50)",  "color": "#ef5350", "kind": "sma", "n": 50},
        {"key": "sma150", "label": "SMA(150)", "color": "#ab47bc", "kind": "sma", "n": 150},
        {"key": "sma200", "label": "SMA(200)", "color": "#26a69a", "kind": "sma", "n": 200},
    ],
}


def compute_stock(ticker, uid, freq="weekly"):
    """OHLCV chart for ONE stock at weekly or daily resolution + the timeframe's
    moving averages + volume + the user's own buy/sell markers. Each bar carries
    the data the detail box shows: OHLC, %Chg vs prior bar, close-range, volume,
    and Vol% (volume vs trailing 50-day average daily volume)."""
    ticker = (ticker or "").upper()
    freq = "daily" if freq == "daily" else "weekly"
    ohlcv = db.get_cached_ohlcv(ticker)
    if not ohlcv:
        return {"ticker": ticker, "freq": freq, "has_data": False,
                "candles": [], "volume": [], "bars": [], "mas": [], "markers": []}

    df = pd.DataFrame.from_dict(ohlcv, orient="index")
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    for col in ("open", "high", "low"):           # legacy close-only rows
        df[col] = df[col].fillna(df["close"])
    df["volume"] = df["volume"].fillna(0.0)

    # Vol% always uses the daily basis (MarketSurge): a day's volume vs the
    # trailing 50-trading-day average daily volume.
    daily_volpct = ((df["volume"] - df["volume"].rolling(50, min_periods=10).mean())
                    / df["volume"].rolling(50, min_periods=10).mean() * 100.0)

    if freq == "weekly":
        w = df.resample("W-FRI")
        bar_df = pd.DataFrame({
            "open": w["open"].first(), "high": w["high"].max(),
            "low": w["low"].min(), "close": w["close"].last(),
            "volume": w["volume"].sum(),
        }).dropna(subset=["close"])
        # map each weekly bar to its last trading day for the daily-basis Vol%
        last_day = df.index.to_series().resample("W-FRI").max()
        volpct_for = lambda ts: (daily_volpct.get(last_day.get(ts))
                                 if last_day.get(ts) is not None else None)
    else:
        bar_df = df[["open", "high", "low", "close", "volume"]].copy()
        volpct_for = lambda ts: daily_volpct.get(ts)

    bar_df["prev_close"] = bar_df["close"].shift(1)

    # moving averages for this timeframe
    ma_specs = _MA_SETS[freq]
    for spec in ma_specs:
        c = bar_df["close"]
        if spec["kind"] == "ema":
            bar_df[spec["key"]] = c.ewm(span=spec["n"], adjust=False,
                                        min_periods=spec["n"]).mean()
        else:
            bar_df[spec["key"]] = c.rolling(spec["n"], min_periods=spec["n"]).mean()

    candles, volume, bars = [], [], []
    for ts, r in bar_df.iterrows():
        if pd.isna(r["close"]):
            continue
        t = ts.strftime("%Y-%m-%d")
        hi, lo, cl, op = r["high"], r["low"], r["close"], r["open"]
        rng = hi - lo
        cls_range = ((cl - lo) / rng * 100.0) if rng and rng > 0 else None
        chg = (cl - r["prev_close"]) if pd.notna(r["prev_close"]) else None
        chg_pct = (chg / r["prev_close"] * 100.0) if (chg is not None and r["prev_close"]) else None
        vol = float(r["volume"]) if pd.notna(r["volume"]) else None
        vpv = volpct_for(ts)
        vol_pct = float(vpv) if vpv is not None and pd.notna(vpv) else None
        up = cl >= op
        candles.append({"time": t, "open": _round(op), "high": _round(hi),
                        "low": _round(lo), "close": _round(cl)})
        if vol is not None:
            volume.append({"time": t, "value": vol,
                           "color": "rgba(38,166,154,.5)" if up else "rgba(239,83,80,.5)"})
        ma_vals = {spec["key"]: (_round(r[spec["key"]]) if pd.notna(r[spec["key"]]) else None)
                   for spec in ma_specs}
        bars.append({
            "time": t, "open": _round(op), "high": _round(hi), "low": _round(lo),
            "close": _round(cl), "chg": _round(chg), "chg_pct": _round(chg_pct),
            "cls_range": _round(cls_range, 0), "volume": vol,
            "vol_pct": _round(vol_pct, 1), "ma": ma_vals,
        })

    # the line series for each MA (key/label/color + points)
    mas = [{
        "key": spec["key"], "label": spec["label"], "color": spec["color"],
        "points": _series_to_points(bar_df[spec["key"]]),
    } for spec in ma_specs]

    # the user's own trades on this ticker, snapped onto bars
    bar_index = pd.DatetimeIndex(bar_df.index)
    markers = []
    for pf in db.list_portfolios(uid):
        for t in db.list_trades(pf["id"]):
            if t["ticker"].upper() != ticker:
                continue
            is_buy = t["side"] == "buy"
            bar = _align(pd.to_datetime(t["date"]), bar_index)
            markers.append({
                "time": bar.strftime("%Y-%m-%d"),
                "position": "belowBar" if is_buy else "aboveBar",
                "color": "#26a69a" if is_buy else "#ef5350",
                "shape": "arrowUp" if is_buy else "arrowDown",
                "text": f"{'B' if is_buy else 'S'} {_round(t['shares'], 2)}@{_round(t['price'], 2)}",
            })
    markers.sort(key=lambda m: m["time"])

    return {
        "ticker": ticker, "freq": freq, "has_data": bool(candles),
        "last": _round(float(df["close"].iloc[-1])),
        "asof": df.index.max().strftime("%Y-%m-%d"),
        "candles": candles, "volume": volume, "bars": bars,
        "mas": mas, "markers": markers,
    }


def _empty(settings, pinfo=None):
    return {
        "has_data": False,
        "portfolio": pinfo,
        "settings": _settings_out(settings),
        "tickers": [], "errors": {}, "warnings": [],
        "weekly": {"candles": [], "ma10": [], "ma40": [], "volume": [], "markers": []},
        "daily": {"line": [], "ma50": [], "volume": [], "markers": []},
        "stats": {"holdings": [], "totals": {}},
        "range": None,
    }


def _settings_out(settings):
    return {
        "starting_capital": float(settings.get("starting_capital") or 0),
        "display_mode": settings.get("display_mode") or "pnl",
    }


def _holding_stats(df: pd.DataFrame, price_today: dict):
    """Average-cost accounting per ticker -> holdings + realized/unrealized P&L."""
    holdings, realized_total = [], 0.0
    for ticker, tdf in df.groupby("ticker"):
        cost_basis, pos = 0.0, 0.0
        realized = 0.0
        for _, r in tdf.sort_values(["date", "id"]).iterrows():
            if r["side"] == "buy":
                cost_basis += r["shares"] * r["price"] + r["fees"]
                pos += r["shares"]
            else:  # sell
                avg = cost_basis / pos if pos > 1e-12 else 0.0
                sold = min(r["shares"], pos) if pos > 0 else r["shares"]
                realized += (r["price"] - avg) * sold - r["fees"]
                cost_basis -= avg * sold
                pos -= r["shares"]
        realized_total += realized
        if pos > 1e-9:  # still holding
            last = price_today.get(ticker)
            avg_cost = cost_basis / pos if pos > 1e-12 else None
            mv = pos * last if last is not None else None
            unreal = (mv - cost_basis) if mv is not None else None
            holdings.append({
                "ticker": ticker,
                "shares": _round(pos, 4),
                "avg_cost": _round(avg_cost, 4),
                "last_price": _round(last, 4),
                "market_value": _round(mv, 2),
                "unrealized": _round(unreal, 2),
                "unrealized_pct": _round((unreal / cost_basis * 100) if (unreal is not None and cost_basis > 1e-9) else None, 2),
            })
    holdings.sort(key=lambda h: (h["market_value"] or 0), reverse=True)
    return holdings, realized_total


def compute(portfolio_id, uid):
    """Chart + stats for a single portfolio (must belong to user `uid`)."""
    p = db.get_portfolio(portfolio_id, uid)
    if not p:
        return _empty({"starting_capital": 0, "display_mode": "value"},
                      {"id": portfolio_id, "name": None})
    pinfo = {"id": portfolio_id, "name": p["name"]}
    return _compute_series(db.list_trades(portfolio_id),
                           float(p["starting_capital"] or 0),
                           p["display_mode"] or "value", pinfo)


def compute_all(uid):
    """Combined overview: all of the user's portfolios merged into one curve.

    Valid because total value is additive — merged cash = Σ per-portfolio cash,
    merged holdings MV = Σ per-portfolio MV — so the merged series equals the
    sum of the individual portfolio series.
    """
    pfs = db.list_portfolios(uid)
    cap = sum(float(p["starting_capital"] or 0) for p in pfs)
    mode = db.get_all_display_mode(uid)
    pinfo = {"id": "all", "name": "全部组合", "count": len(pfs)}
    return _compute_series(db.list_all_trades(uid), cap, mode, pinfo)


def _compute_series(trades, starting_capital, mode, pinfo):
    settings = {"starting_capital": starting_capital, "display_mode": mode}
    if not trades:
        return _empty(settings, pinfo)

    starting_capital = float(starting_capital or 0)
    mode = mode or "value"

    df = pd.DataFrame(trades)
    df["date"] = pd.to_datetime(df["date"])
    df["ticker"] = df["ticker"].str.upper()
    df["side"] = df["side"].str.lower()
    df["signed_shares"] = df.apply(
        lambda r: r["shares"] if r["side"] == "buy" else -r["shares"], axis=1)
    df["cashflow"] = df.apply(
        lambda r: (-(r["shares"] * r["price"]) - r["fees"]) if r["side"] == "buy"
        else ((r["shares"] * r["price"]) - r["fees"]), axis=1)
    df["notional"] = df["shares"] * df["price"]

    tickers = sorted(df["ticker"].unique().tolist())
    start = df["date"].min().strftime("%Y-%m-%d")
    end = dt.date.today().isoformat()

    price_series, errors = prices.get_daily_closes(tickers, start, end)
    warnings = []

    # ---- build the daily valuation calendar (trading days) -----------------
    idxs = [s.index for s in price_series.values() if len(s)]
    if idxs:
        daily_index = idxs[0]
        for i in idxs[1:]:
            daily_index = daily_index.union(i)
    else:
        daily_index = pd.bdate_range(start, end)
        warnings.append("No price data for any ticker — chart shows cash flow only.")
    first_trade = df["date"].min()
    # Always include every business day from the first trade through today, even
    # if historical price data hasn't published the current week yet — those
    # days are valued with the latest known price (forward-filled below). This
    # keeps the account curve continuous from the first trade onward instead of
    # collapsing to one stub bar when holdings were just imported this week.
    bdays = pd.bdate_range(first_trade, pd.Timestamp(end))
    daily_index = daily_index.union(bdays)
    daily_index = daily_index[(daily_index >= first_trade) &
                              (daily_index <= pd.Timestamp(end))]
    daily_index = pd.DatetimeIndex(pd.Series(daily_index).sort_values().unique())
    if len(daily_index) == 0:
        daily_index = pd.DatetimeIndex([first_trade])

    # ---- daily holdings market value ---------------------------------------
    holdings_mv = pd.Series(0.0, index=daily_index)
    price_today = {}
    for t in tickers:
        tdf = df[df["ticker"] == t]
        cum = tdf.groupby("date")["signed_shares"].sum().sort_index().cumsum()
        shares_daily = (cum.reindex(cum.index.union(daily_index)).ffill()
                        .reindex(daily_index).fillna(0.0))
        if t in price_series:
            # latest known close — independent of the valuation calendar, so a
            # holding still values correctly when its trade date is newer than
            # the most recent available close (e.g. bought today, pre-close).
            price_today[t] = float(price_series[t].dropna().iloc[-1])
            # align prices onto the calendar; ffill carries the last close
            # forward (covers a trade date past the latest close), bfill covers
            # any leading gap before the first close.
            p_daily = price_series[t].reindex(
                price_series[t].index.union(daily_index)).ffill().bfill()
            p_daily = p_daily.reindex(daily_index)
        else:
            p_daily = pd.Series(float("nan"), index=daily_index)
        holdings_mv = holdings_mv.add((shares_daily * p_daily).fillna(0.0),
                                      fill_value=0.0)

    # ---- daily cash flow & value series ------------------------------------
    cf = df.groupby("date")["cashflow"].sum().sort_index().cumsum()
    cf_daily = (cf.reindex(cf.index.union(daily_index)).ffill()
                .reindex(daily_index).fillna(0.0))

    pnl_daily = cf_daily + holdings_mv               # capital-independent P&L
    value_daily = starting_capital + pnl_daily       # account value / NAV

    if mode == "value":
        series_daily, unit = value_daily, "$"
    elif mode == "index":
        base = value_daily.iloc[0]
        if base and base > 0:
            series_daily, unit = (value_daily / base * 100.0), "idx"
        else:
            series_daily, unit = pnl_daily, "$"
            warnings.append("Set a starting capital (> 0) to use the indexed view; "
                            "showing P&L instead.")
            mode = "pnl"
    else:
        series_daily, unit = pnl_daily, "$"
        mode = "pnl"

    # ---- weekly OHLC candles + moving averages -----------------------------
    w = series_daily.resample("W-FRI")
    wk = pd.DataFrame({
        "open": w.first(), "high": w.max(), "low": w.min(), "close": w.last(),
    }).dropna(how="all")
    weekly_index = pd.DatetimeIndex(wk.index)
    candles = [{
        "time": ts.strftime("%Y-%m-%d"),
        "open": _round(row["open"]), "high": _round(row["high"]),
        "low": _round(row["low"]), "close": _round(row["close"]),
    } for ts, row in wk.iterrows() if not math.isnan(row["close"])]

    ma10 = _series_to_points(wk["close"].rolling(10, min_periods=10).mean())
    ma40 = _series_to_points(wk["close"].rolling(40, min_periods=40).mean())

    # ---- weekly volume + markers from the user's own trades ----------------
    df["wk_time"] = df["date"].apply(lambda d: _align(d, weekly_index).strftime("%Y-%m-%d"))
    weekly = _activity(df, "wk_time")
    weekly_markers = _markers(df, "wk_time")

    # ---- daily line view ----------------------------------------------------
    daily_line = _series_to_points(series_daily)
    ma50 = _series_to_points(series_daily.rolling(50, min_periods=50).mean())
    df["d_time"] = df["date"].apply(lambda d: _align(d, daily_index).strftime("%Y-%m-%d"))
    daily_vol = _activity(df, "d_time")
    daily_markers = _markers(df, "d_time")

    # ---- stats --------------------------------------------------------------
    holdings, realized_total = _holding_stats(df, price_today)
    mv_total = sum((h["market_value"] or 0) for h in holdings)
    unrealized_total = sum((h["unrealized"] or 0) for h in holdings)
    cash = starting_capital + float(cf_daily.iloc[-1])
    total_value = cash + mv_total
    total_pnl = float(pnl_daily.iloc[-1])

    # ---- position weight & O'Neil risk exposure ----------------------------
    # weight    = position market value / total account value (cash + holdings)
    # risk_8pct = account % lost if this stock drops 8% (= weight * 8%), i.e.
    #             the portfolio "heat" this position carries at an 8% stop.
    STOP = 8.0
    base = total_value if total_value and total_value > 0 else mv_total
    for h in holdings:
        mv = h["market_value"] or 0
        w = (mv / base * 100.0) if base and base > 0 else None
        h["weight"] = _round(w, 2)
        h["risk_8pct"] = _round((w * STOP / 100.0) if w is not None else None, 2)
    buy_cost = float(df.loc[df["side"] == "buy", "notional"].sum() +
                     df.loc[df["side"] == "buy", "fees"].sum())
    invested = starting_capital if starting_capital > 0 else buy_cost
    totals = {
        "market_value": _round(mv_total),
        "cash": _round(cash),
        "total_value": _round(total_value),
        "realized_pnl": _round(realized_total),
        "unrealized_pnl": _round(unrealized_total),
        "total_pnl": _round(total_pnl),
        "invested": _round(invested),
        "return_pct": _round((total_pnl / invested * 100) if invested > 0 else None),
        "num_trades": len(df),
        "num_positions": len(holdings),
        # exposure rollups
        "invested_pct": _round(sum((h["weight"] or 0) for h in holdings), 2),
        "total_risk_8pct": _round(sum((h["risk_8pct"] or 0) for h in holdings), 2),
    }

    return {
        "has_data": True,
        "portfolio": pinfo,
        "settings": _settings_out({**settings, "display_mode": mode}),
        "mode": mode, "unit": unit,
        "tickers": tickers, "errors": errors, "warnings": warnings,
        "range": {"start": start, "end": end},
        "weekly": {"candles": candles, "ma10": ma10, "ma40": ma40,
                   "volume": weekly, "markers": weekly_markers},
        "daily": {"line": daily_line, "ma50": ma50,
                  "volume": daily_vol, "markers": daily_markers},
        "stats": {"holdings": holdings, "totals": totals},
    }


def _activity(df: pd.DataFrame, time_col: str):
    """Volume bars from the user's own buy/sell notional per bucket."""
    out = []
    for tkey, g in df.groupby(time_col):
        buys = g.loc[g["side"] == "buy", "notional"].sum()
        sells = g.loc[g["side"] == "sell", "notional"].sum()
        total = float(buys + sells)
        net = float(buys - sells)
        color = "#26a69a" if net > 0 else ("#ef5350" if net < 0 else "#787b86")
        out.append({"time": tkey, "value": _round(total), "color": color})
    out.sort(key=lambda x: x["time"])
    return out


def _markers(df: pd.DataFrame, time_col: str):
    """One marker per trade with the reason carried along for the UI."""
    out = []
    for _, r in df.sort_values(["date", "id"]).iterrows():
        is_buy = r["side"] == "buy"
        out.append({
            "time": r[time_col],
            "position": "belowBar" if is_buy else "aboveBar",
            "color": "#26a69a" if is_buy else "#ef5350",
            "shape": "arrowUp" if is_buy else "arrowDown",
            "text": r["ticker"],  # short label; full details + reason show on hover
            # extra fields for the detail panel (ignored by the chart lib):
            "id": int(r["id"]), "date": r["date"].strftime("%Y-%m-%d"),
            "ticker": r["ticker"], "side": r["side"],
            "shares": _round(r["shares"], 4), "price": _round(r["price"], 4),
            "reason": r["reason"] or "",
            "portfolio": (r.get("portfolio_name") or ""),  # set in combined view
        })
    return out
