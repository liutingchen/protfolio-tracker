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
        "stats": {"holdings": [], "totals": {}, "closed": []},
        "range": None,
    }


def _settings_out(settings):
    return {
        "starting_capital": float(settings.get("starting_capital") or 0),
        "display_mode": settings.get("display_mode") or "pnl",
    }


def _holding_stats(df: pd.DataFrame, price_today: dict, notes=None):
    """Average-cost accounting per ticker -> holdings + realized/unrealized P&L.
    Also returns `closed`: per-ticker realized P&L for any ticker that has been
    sold (fully or partially), with the cost basis of the sold shares so a
    return% can be shown."""
    holdings, realized_total, closed = [], 0.0, []
    for ticker, tdf in df.groupby("ticker"):
        cost_basis, pos = 0.0, 0.0
        realized = 0.0
        sold_cost = 0.0           # avg-cost basis of all shares sold (for return%)
        sold_shares = 0.0
        sold_proceeds = 0.0       # gross sell proceeds (for avg sell price)
        last_sell = None          # most recent sell date
        lots = []                 # one entry per sell (a "round") for the breakdown
        for _, r in tdf.sort_values(["date", "id"]).iterrows():
            if r["side"] == "buy":
                cost_basis += r["shares"] * r["price"] + r["fees"]
                pos += r["shares"]
            else:  # sell
                avg = cost_basis / pos if pos > 1e-12 else 0.0
                sold = min(r["shares"], pos) if pos > 0 else r["shares"]
                sell_realized = (r["price"] - avg) * sold - r["fees"]
                realized += sell_realized
                cost_basis -= avg * sold
                pos -= r["shares"]
                sold_cost += avg * sold
                sold_shares += sold
                sold_proceeds += r["price"] * sold
                last_sell = r["date"]
                lots.append({
                    "date": (r["date"].strftime("%Y-%m-%d")
                             if hasattr(r["date"], "strftime") else str(r["date"])),
                    "shares": _round(sold, 4),
                    "buy": _round(avg, 4),          # avg cost of the shares sold here
                    "sell": _round(r["price"], 4),
                    "realized": _round(sell_realized, 2),
                    "return_pct": _round((sell_realized / (avg * sold) * 100)
                                         if (avg * sold) > 1e-9 else None, 2),
                })
        realized_total += realized
        if sold_shares > 1e-9:    # this ticker had at least one sell
            closed.append({
                "ticker": ticker,
                "realized": _round(realized, 2),
                "sold_shares": _round(sold_shares, 4),
                "cost_basis_sold": _round(sold_cost, 2),
                "avg_buy": _round(sold_cost / sold_shares, 4) if sold_shares > 1e-9 else None,
                "avg_sell": _round(sold_proceeds / sold_shares, 4) if sold_shares > 1e-9 else None,
                "return_pct": _round((realized / sold_cost * 100) if sold_cost > 1e-9 else None, 2),
                "last_sell": (last_sell.strftime("%Y-%m-%d")
                              if hasattr(last_sell, "strftime") else str(last_sell)),
                "still_holding": pos > 1e-9,
                "note": (notes or {}).get(ticker, ""),
                "lots": lots,            # per-sell breakdown (round-by-round)
            })
        if pos > 1e-9:  # still holding
            last = price_today.get(ticker)
            avg_cost = cost_basis / pos if pos > 1e-12 else None
            mv = pos * last if last is not None else None
            unreal = (mv - cost_basis) if mv is not None else None
            # which portfolios still hold this ticker (combined view only).
            # A portfolio that fully sold out is excluded.
            portfolios = []
            if "portfolio_name" in tdf.columns:
                for pname, ptdf in tdf.groupby("portfolio_name"):
                    net = ptdf.apply(lambda r: r["shares"] if r["side"] == "buy"
                                     else -r["shares"], axis=1).sum()
                    if net > 1e-9:
                        portfolios.append((pname, net))
                portfolios.sort(key=lambda x: -x[1])   # biggest position first
            holdings.append({
                "ticker": ticker,
                "shares": _round(pos, 4),
                "avg_cost": _round(avg_cost, 4),
                "last_price": _round(last, 4),
                "market_value": _round(mv, 2),
                "unrealized": _round(unreal, 2),
                "unrealized_pct": _round((unreal / cost_basis * 100) if (unreal is not None and cost_basis > 1e-9) else None, 2),
                "portfolios": [p[0] for p in portfolios],
            })
    holdings.sort(key=lambda h: (h["market_value"] or 0), reverse=True)
    closed.sort(key=lambda x: x["last_sell"], reverse=True)   # most recent sells first
    return holdings, realized_total, closed


def compute(portfolio_id, uid):
    """Chart + stats for a single portfolio (must belong to user `uid`)."""
    p = db.get_portfolio(portfolio_id, uid)
    if not p:
        return _empty({"starting_capital": 0, "display_mode": "value"},
                      {"id": portfolio_id, "name": None})
    pinfo = {"id": portfolio_id, "name": p["name"]}
    return _compute_series(db.list_trades(portfolio_id),
                           float(p["starting_capital"] or 0),
                           p["display_mode"] or "value", pinfo,
                           db.get_review_notes(uid))


def compute_all(uid, portfolio_ids=None, name="全部组合", gid=None):
    """Combined overview: merge all of the user's portfolios (or a chosen subset
    for a custom group) into one curve.

    Valid because total value is additive — merged cash = Σ per-portfolio cash,
    merged holdings MV = Σ per-portfolio MV — so the merged series equals the
    sum of the individual portfolio series.
    """
    pfs = db.list_portfolios(uid)
    if portfolio_ids is not None:
        idset = {int(i) for i in portfolio_ids}
        pfs = [p for p in pfs if p["id"] in idset]
    cap = sum(float(p["starting_capital"] or 0) for p in pfs)
    mode = db.get_all_display_mode(uid)
    pinfo = {"id": (f"group:{gid}" if gid is not None else "all"),
             "name": name, "count": len(pfs)}
    return _compute_series(db.list_all_trades(uid, portfolio_ids), cap, mode, pinfo,
                           db.get_review_notes(uid))


def _compute_series(trades, starting_capital, mode, pinfo, notes=None):
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

    # All valuation uses the regular-session close. Pre/post is shown as a
    # second line (like brokers: "Today" + "After-hours"), never mixed in.
    qmeta = db.get_quote_meta()
    _sess = next((qmeta[t].get("session") for t in qmeta), None)
    ext_active = _sess in ("pre", "post")

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
    holdings, realized_total, closed = _holding_stats(df, price_today, notes)
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

    # ---- daily change + after-hours per holding ----------------------------
    # Broker-correct day P&L: shares held coming into the current session are
    # marked from the prior close; shares TRADED in the current session are
    # marked from their actual trade price (a position bought today only counts
    # the move since you bought it, not the whole day's move).
    #   day P&L = current_value - prior_close_value - net_cash_spent_today
    # "Current session" is anchored to the latest available trading day (the last
    # date in the price data), NOT the server's wall-clock today() — the server
    # runs in UTC and may be a day ahead, and you may enter trades over a weekend.
    # A trade on/after that latest trading day is treated as a current-session trade.
    price_dates = [s.index.max() for s in price_series.values() if len(s)]
    cur_day = max(price_dates) if price_dates else pd.Timestamp(dt.date.today())
    cur_mask = df["date"] >= cur_day
    cur_grp = df[cur_mask].groupby("ticker")
    # net shares bought today, and net $ spent on today's trades (buys +, sells -)
    today_net_sh = (cur_grp.apply(lambda g: (g.loc[g["side"] == "buy", "shares"].sum()
                                             - g.loc[g["side"] == "sell", "shares"].sum()))
                    if cur_mask.any() else pd.Series(dtype="float64"))
    today_net_cash = (cur_grp.apply(lambda g: (g.loc[g["side"] == "buy", "notional"].sum()
                                               - g.loc[g["side"] == "sell", "notional"].sum()))
                      if cur_mask.any() else pd.Series(dtype="float64"))

    day_chg_total = 0.0
    ext_chg_total = 0.0
    for h in holdings:
        q = qmeta.get(h["ticker"])
        shares = h["shares"] or 0
        pc = q.get("prev_close") if q else None
        last = h["last_price"]            # regular-session close
        if pc and last is not None:
            net_sh_today = float(today_net_sh.get(h["ticker"], 0.0) or 0.0)
            net_cash_today = float(today_net_cash.get(h["ticker"], 0.0) or 0.0)
            pos_yest = shares - net_sh_today          # shares held coming into today
            cur_value = last * shares
            prior_value = pc * pos_yest
            day_chg = cur_value - prior_value - net_cash_today
            h["day_chg"] = _round(day_chg, 2)
            # % is vs the day's starting basis (prior close value + cash put in today)
            basis = prior_value + max(net_cash_today, 0.0)
            h["day_chg_pct"] = _round((day_chg / basis * 100.0) if basis > 1e-9 else None, 2)
            day_chg_total += day_chg
        else:
            h["day_chg"] = None
            h["day_chg_pct"] = None
        # Pre/post shown as a SECOND line (broker style: "Today" + "After-hours").
        # The after-hours line = just the extended-session segment:
        #   ext_price = the pre/post price
        #   ext_chg   = (ext_price - regular_close) * shares   ($ for the period)
        #   ext_chg_pct = % move of the extended session vs the regular close
        h["session"] = q.get("session") if q else None
        ep = q.get("ext_price") if q else None
        if ext_active and ep is not None and last is not None:
            h["ext_price"] = _round(ep, 4)
            h["ext_chg_pct"] = _round(q.get("ext_chg_pct"), 2)
            h["ext_chg"] = _round((ep - last) * shares, 2)          # 当日列的盘后行 ($)
            h["ext_mv"] = _round(ep * shares, 2)                     # 市值列的盘后行
            h["ext_unreal"] = _round(ep * shares - (h["avg_cost"] or 0) * shares, 2)  # 浮动列的盘后行
            ext_chg_total += (ep - last) * shares
        else:
            h["ext_price"] = None
            h["ext_chg_pct"] = None
            h["ext_chg"] = None
            h["ext_mv"] = None
            h["ext_unreal"] = None

    # ---- drawdown stats (always based on dollar value_daily / NAV) ----------
    # All-time high: the highest account value ever recorded in the daily series.
    ath = float(value_daily.max())
    ath_dd_pct = _round((total_value - ath) / ath * 100, 2) if ath > 1e-6 else None

    # Previous-week high: max NAV over the 5 trading days immediately preceding
    # the most recent data point (rolling "last week" window).
    if len(value_daily) > 1:
        prev_slice = value_daily.iloc[max(0, len(value_daily) - 6):-1]
        week_high = float(prev_slice.max()) if len(prev_slice) > 0 else None
    else:
        week_high = None
    week_dd_pct = (_round((total_value - week_high) / week_high * 100, 2)
                   if (week_high and week_high > 1e-6) else None)

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
        # today's account P&L (regular session: close vs prev close)
        "day_pnl": _round(day_chg_total, 2),
        "day_pnl_pct": _round((day_chg_total / (total_value - day_chg_total) * 100.0)
                              if (total_value - day_chg_total) else None, 2),
        "is_ext": ext_active,   # a pre/post session is active -> show 2nd line
        # drawdown from historical peak and previous-week peak
        "ath": _round(ath, 2),
        "ath_dd_pct": ath_dd_pct,          # 0 = AT ATH; negative = below ATH
        "week_high": _round(week_high, 2) if week_high else None,
        "week_dd_pct": week_dd_pct,        # 0 = at week high; negative = below
    }
    # after-hours SECOND line (broker style): the extended segment only.
    if ext_active and ext_chg_total:
        totals["ext_pnl"] = _round(ext_chg_total, 2)               # 当日行下的盘后行
        totals["ext_pnl_pct"] = _round((ext_chg_total / total_value * 100.0)
                                       if total_value else None, 2)
        totals["market_value_ext"] = _round(mv_total + ext_chg_total, 2)
        totals["total_value_ext"] = _round(total_value + ext_chg_total, 2)

    # overall market status (from any quote meta — they share the session/market)
    _mkt = next(iter(db.get_quote_meta().values()), None)
    market_status = {"session": _mkt.get("session"), "market": _mkt.get("market"),
                     "as_of": _mkt.get("as_of")} if _mkt else None

    return {
        "has_data": True,
        "portfolio": pinfo,
        "market_status": market_status,
        "settings": _settings_out({**settings, "display_mode": mode}),
        "mode": mode, "unit": unit,
        "tickers": tickers, "errors": errors, "warnings": warnings,
        "range": {"start": start, "end": end},
        "weekly": {"candles": candles, "ma10": ma10, "ma40": ma40,
                   "volume": weekly, "markers": weekly_markers},
        "daily": {"line": daily_line, "ma50": ma50,
                  "volume": daily_vol, "markers": daily_markers},
        "stats": {"holdings": holdings, "totals": totals, "closed": closed},
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
