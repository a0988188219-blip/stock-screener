# -*- coding: utf-8 -*-
"""
台股每日盤後選股 + 持股檢查 → LINE 推播（上市股票）
適合抱 1～4 週的波段操作

每天兩種名單：
  🚀 突破型：帶量突破壓力、盤整突破
  🔄 拉回支撐型：多頭趨勢中拉回到支撐，量縮止跌
每檔都附上支撐、壓力與進場時機判斷

突破型評分（滿分 16）：
  技術面 最多 9 分：均線多頭排列、月線上揚、創高、盤整突破、收紅K
  量能   最多 2 分：爆量 / 量增價漲
  籌碼面 最多 5 分：外資買、投信買、投信連買、法人同步買
"""
import os, sys, time, json, datetime as dt
import requests
import pandas as pd
import yfinance as yf

# ================== 可以自己調整的條件 ==================
MIN_AVG_VOLUME_LOTS = 1000      # 20 日平均成交量至少幾張
MIN_PRICE = 10                  # 股價下限
MAX_PRICE = 1000                # 股價上限
MIN_SCORE = 8                   # 幾分以上才列入
TOP_N = 10                      # 每天最多推幾檔
CONSOLIDATION_MAX_RANGE = 15    # 盤整區間：最高最低差在幾 % 以內才算盤整
ONLY_CONSOLIDATION = False      # 改成 True 就只推「盤整突破」的股票
TRUST_STREAK_DAYS = 3           # 投信連買幾天以上加分
CHIP_DAYS = 10                  # 抓最近幾天的法人資料
HOLDINGS_FILE = "holdings.txt"  # 持股清單檔
PULLBACK_TOP_N = 5              # 拉回支撐名單最多幾檔
PULLBACK_MAX_DIST = 3           # 收盤離支撐幾 % 以內算「回到支撐」
# =======================================================

TW = dt.timezone(dt.timedelta(hours=8))
HEADERS = {"User-Agent": "Mozilla/5.0"}


def to_num(x):
    try:
        return float(str(x).replace(",", "").strip())
    except Exception:
        return None


# ---------------- 抓資料 ----------------
def get_universe():
    """回傳 (全部上市股票名稱, 要下載的代號)"""
    r = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
                     headers=HEADERS, timeout=30)
    names, candidates = {}, []
    for row in r.json():
        code = str(row.get("Code", ""))
        if len(code) == 4 and code.isdigit() and not code.startswith("0"):
            names[code] = row.get("Name", "")
            candidates.append(code)
    return names, candidates


def get_prices(codes):
    result = {}
    tickers = [c + ".TW" for c in codes]
    for i in range(0, len(tickers), 100):
        batch = tickers[i:i + 100]
        data = yf.download(batch, period="9mo", interval="1d", group_by="ticker",
                           auto_adjust=False, threads=True, progress=False)
        for t in batch:
            try:
                df = data[t][["Open", "High", "Low", "Close", "Volume"]].dropna()
                if len(df):
                    result[t[:-3]] = df
            except Exception:
                pass
        time.sleep(2)
    return result


def get_single_price(code):
    """持股用：上市找不到就試上櫃"""
    for suffix in (".TW", ".TWO"):
        try:
            df = yf.Ticker(code + suffix).history(period="9mo", auto_adjust=False)
            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
            if len(df):
                df.index = df.index.tz_localize(None)
                return df
        except Exception:
            pass
    return None


def get_chips_one(date_str):
    """某一天證交所三大法人買賣超（單位：張）"""
    url = f"https://www.twse.com.tw/rwd/zh/fund/T86?date={date_str}&selectType=ALLBUT0999&response=json"
    try:
        j = requests.get(url, headers=HEADERS, timeout=30).json()
        f = j["fields"]
        i_code = f.index("證券代號")
        i_for = next(i for i, x in enumerate(f) if x.startswith("外陸資買賣超"))
        i_tru = next(i for i, x in enumerate(f) if x.startswith("投信買賣超"))
        i_tot = next(i for i, x in enumerate(f) if x.startswith("三大法人買賣超"))
        return {row[i_code].strip(): {"foreign": to_num(row[i_for]) / 1000,
                                      "trust": to_num(row[i_tru]) / 1000,
                                      "total": to_num(row[i_tot]) / 1000}
                for row in j["data"]}
    except Exception as e:
        print(f"{date_str} 法人資料抓取失敗：", e)
        return None


def get_chip_history(dates):
    hist = []
    for d in dates:
        hist.append(get_chips_one(d.strftime("%Y%m%d")))
        time.sleep(3)   # 證交所抓太快會被擋
    return hist


def chip_series(hist, code, key):
    return [(d.get(code) or {}).get(key, 0) for d in hist if d is not None]


def streak(vals, positive=True):
    n = 0
    for x in reversed(vals):
        if (x > 0) if positive else (x < 0):
            n += 1
        else:
            break
    return n


# ---------------- 支撐與壓力 ----------------
def pivots(df, n=5, lookback=120):
    """找轉折高點、低點：前後各 n 天裡最高/最低的那一天"""
    d = df.iloc[-lookback:]
    H, L = d["High"].values, d["Low"].values
    highs, lows = [], []
    for i in range(n, len(d) - n):
        if H[i] == H[i - n:i + n + 1].max():
            highs.append(float(H[i]))
        if L[i] == L[i - n:i + n + 1].min():
            lows.append(float(L[i]))
    return highs, lows


def find_levels(df):
    """
    壓力：上方最近的前波高點
    支撐：下方最近的 前波低點 / 已突破的前波高點（壓力變支撐）/ 月線
    """
    close = float(df["Close"].iloc[-1])
    ma20 = float(df["Close"].rolling(20).mean().iloc[-1])
    highs, lows = pivots(df)
    above = [h for h in highs if h > close * 1.01]
    resistance = min(above) if above else None
    below = [l for l in lows if l < close] + [h for h in highs if h < close * 0.99]
    if ma20 < close:
        below.append(ma20)
    support = max(below) if below else None
    return support, resistance


def timing_notes(close, support, resistance):
    """把支撐壓力翻成白話的進場判斷"""
    notes = []
    if support:
        dist = (close / support - 1) * 100
        if dist <= 5:
            notes.append(f"離支撐{dist:.1f}%，風險好控制")
        elif dist > 10:
            notes.append(f"離支撐{dist:.0f}%偏遠，可等拉回")
        else:
            notes.append(f"離支撐{dist:.1f}%")
    if resistance is None:
        notes.append("上方無前高壓力")
    elif support and close > support:
        rr = (resistance - close) / (close - support)
        if rr >= 2:
            notes.append(f"上漲空間是風險的{rr:.1f}倍")
        elif rr < 1:
            notes.append("離壓力近，空間有限")
        else:
            notes.append(f"空間/風險 {rr:.1f}倍")
    return notes


def analyze_pullback(df, chip, trust_streak):
    """拉回支撐：多頭趨勢中回到支撐附近，量縮、出現止跌K"""
    if len(df) < 65:
        return None
    c, v = df["Close"], df["Volume"]
    ma20, ma60 = c.rolling(20).mean(), c.rolling(60).mean()
    vma20 = v.rolling(20).mean()
    close, op = c.iloc[-1], df["Open"].iloc[-1]
    hi, lo = df["High"].iloc[-1], df["Low"].iloc[-1]
    if vma20.iloc[-2] / 1000 < MIN_AVG_VOLUME_LOTS or not (MIN_PRICE <= close <= MAX_PRICE):
        return None
    # 趨勢要是多頭：月線上揚、月線在季線上、股價在季線上
    if not (ma20.iloc[-1] > ma20.iloc[-6] and ma20.iloc[-1] > ma60.iloc[-1] and close > ma60.iloc[-1]):
        return None
    # 最近 10 天內要有拉回（從高點回落 5% 以上）
    recent_high = df["High"].iloc[-10:].max()
    if close > recent_high * 0.95:
        return None
    support, resistance = find_levels(df)
    if not support:
        return None
    dist = (close / support - 1) * 100
    if not (0 <= dist <= PULLBACK_MAX_DIST):
        return None
    vr = v.iloc[-1] / vma20.iloc[-2]
    if vr > 1.0:
        return None     # 拉回要量縮，爆量下殺不算

    score, tags = 0, [f"回到支撐{support:,.2f}附近"]
    if vr <= 0.6:
        score += 2; tags.append(f"明顯量縮（{vr:.1f}倍）")
    else:
        score += 1; tags.append(f"量縮（{vr:.1f}倍）")
    body_low = min(op, close)
    if close > op:
        score += 1; tags.append("收紅K")
    if hi > lo and (body_low - lo) / (hi - lo) >= 0.4:
        score += 1; tags.append("長下影線止跌")
    if chip and chip["trust"] > 0:
        score += 1; tags.append(f"投信買{chip['trust']:,.0f}張")
    if trust_streak >= TRUST_STREAK_DAYS:
        score += 2; tags.append(f"投信連買{trust_streak}天")
    if score < 2:
        return None
    return {"close": close, "chg": (close / c.iloc[-2] - 1) * 100, "score": score,
            "tags": tags, "support": support, "resistance": resistance,
            "notes": timing_notes(close, support, resistance), "vr": vr}


# ---------------- 選股邏輯 ----------------
def consolidation_breakout(df, vr):
    """盤整突破：前 N 天在窄區間整理，今天帶量收在區間上緣之上。回傳 (天數, 區間%)"""
    close = df["Close"].iloc[-1]
    for days in (60, 40, 20):          # 先找整理最久的
        if len(df) < days + 1:
            continue
        win = df.iloc[-days - 1:-1]
        top, bottom = win["High"].max(), win["Low"].min()
        rng = (top / bottom - 1) * 100
        if rng <= CONSOLIDATION_MAX_RANGE and close > top and vr >= 1.5:
            return days, rng
    return None


def analyze(df, chip, trust_streak):
    if len(df) < 65:
        return None
    c, v = df["Close"], df["Volume"]
    ma5, ma20, ma60 = c.rolling(5).mean(), c.rolling(20).mean(), c.rolling(60).mean()
    vma20 = v.rolling(20).mean()

    close, prev = c.iloc[-1], c.iloc[-2]
    op, hi, lo = df["Open"].iloc[-1], df["High"].iloc[-1], df["Low"].iloc[-1]
    if vma20.iloc[-2] / 1000 < MIN_AVG_VOLUME_LOTS or not (MIN_PRICE <= close <= MAX_PRICE):
        return None

    chg = (close / prev - 1) * 100
    vr = v.iloc[-1] / vma20.iloc[-2]
    score, tags, warns = 0, [], []

    # ---- 技術面 ----
    if close > ma5.iloc[-1] > ma20.iloc[-1] > ma60.iloc[-1]:
        score += 2; tags.append("均線多頭排列")
    if ma20.iloc[-1] > ma20.iloc[-6]:
        score += 1; tags.append("月線上揚")
    if close > df["High"].iloc[-61:-1].max():
        score += 2; tags.append("創60日新高")
    elif close > df["High"].iloc[-21:-1].max():
        score += 1; tags.append("突破20日高")
    cb = consolidation_breakout(df, vr)
    if cb:
        days, rng = cb
        score += 3 if days >= 40 else 2
        tags.insert(0, f"盤整{days}天突破（區間{rng:.0f}%）")
    if close > op and hi > lo and (close - lo) / (hi - lo) >= 0.7:
        score += 1; tags.append("收紅K近高點")

    # ---- 量能 ----
    if vr >= 2 and chg > 0:
        score += 2; tags.append(f"爆量{vr:.1f}倍")
    elif vr >= 1.5 and chg > 0:
        score += 1; tags.append(f"量增{vr:.1f}倍")

    # ---- 籌碼面 ----
    if chip:
        if chip["foreign"] > 0:
            score += 1; tags.append(f"外資買{chip['foreign']:,.0f}張")
        if chip["trust"] > 0:
            score += 1; tags.append(f"投信買{chip['trust']:,.0f}張")
        if chip["foreign"] > 0 and chip["trust"] > 0 and chip["total"] > 0:
            score += 1; tags.append("法人同步買")
    if trust_streak >= TRUST_STREAK_DAYS:
        score += 2; tags.append(f"投信連買{trust_streak}天")

    # ---- 風險提醒（不扣分）----
    bias = (close / ma20.iloc[-1] - 1) * 100
    if bias > 15:
        warns.append(f"離月線{bias:.0f}%，乖離偏大")
    if chg >= 9.5:
        warns.append("今日漲停，追價留意")

    support, resistance = find_levels(df)
    return {"close": close, "chg": chg, "score": score, "tags": tags, "warns": warns,
            "ma20": ma20.iloc[-1], "vr": vr, "consolidation": cb is not None,
            "support": support, "resistance": resistance,
            "notes": timing_notes(close, support, resistance)}


# ---------------- 持股檢查 ----------------
def read_holdings():
    """持股來源：GitHub 的 HOLDINGS 變數（例：2330,3008,6488），或 holdings.txt"""
    codes = []
    for part in os.getenv("HOLDINGS", "").replace("，", ",").replace(" ", ",").split(","):
        if part.strip():
            codes.append(part.strip())
    if os.path.exists(HOLDINGS_FILE):
        for line in open(HOLDINGS_FILE, encoding="utf-8"):
            code = line.split("#")[0].strip()
            if code:
                codes.append(code)
    return list(dict.fromkeys(codes))


def check_holding(df, hist, code):
    c = df["Close"]
    close, prev = c.iloc[-1], c.iloc[-2]
    ma10, ma20 = c.rolling(10).mean().iloc[-1], c.rolling(20).mean().iloc[-1]
    alerts = []
    if close < ma20:
        alerts.append("跌破月線")
    elif close < ma10:
        alerts.append("跌破10日線，留意")
    n_total = streak(chip_series(hist, code, "total"), positive=False)
    if n_total >= 3:
        alerts.append(f"法人連賣{n_total}天")
    n_trust = streak(chip_series(hist, code, "trust"), positive=False)
    if n_trust >= 3:
        alerts.append(f"投信連賣{n_trust}天")
    support, resistance = find_levels(df)
    return {"close": close, "chg": (close / prev - 1) * 100,
            "bias": (close / ma20 - 1) * 100, "alerts": alerts,
            "support": support, "resistance": resistance}


# ---------------- 網頁健檢 ----------------
def r2(x):
    try:
        x = float(x)
        return None if x != x else round(x, 2)
    except Exception:
        return None


def health_check(df, hist, code, chips_ok):
    """一檔股票的續抱健檢：回傳每個檢查項目 + 總結"""
    c, v = df["Close"], df["Volume"]
    close, prev = float(c.iloc[-1]), float(c.iloc[-2])
    ma5, ma10 = c.rolling(5).mean(), c.rolling(10).mean()
    ma20, ma60 = c.rolling(20).mean(), c.rolling(60).mean()
    m5, m10, m20 = ma5.iloc[-1], ma10.iloc[-1], ma20.iloc[-1]
    m60 = ma60.iloc[-1] if len(df) >= 60 else None
    support, resistance = find_levels(df)
    checks = []

    def add(s, t, d):
        checks.append({"s": s, "t": t, "d": d})

    # 1. 月線趨勢
    rising = ma20.iloc[-1] > ma20.iloc[-6]
    if close >= m20 and rising:
        add("good", "趨勢", f"站在月線（{m20:,.2f}）上，月線還在往上")
    elif close >= m20:
        add("warn", "趨勢", f"還在月線（{m20:,.2f}）上，但月線走平或下彎，動能變弱")
    else:
        add("bad", "趨勢", f"已經跌破月線（{m20:,.2f}）")

    # 2. 均線排列
    if m60 is not None and m5 > m10 > m20 > m60:
        add("good", "均線", "5、10、20、60 日線由上往下排好，多頭排列")
    elif m5 < m10 < m20:
        add("bad", "均線", "短天期均線往下排，短線轉弱")
    else:
        add("warn", "均線", "均線糾結在一起，方向還不明確")

    # 3. 支撐
    if support:
        d = (close / support - 1) * 100
        if d <= 8:
            add("good", "支撐", f"下方支撐在 {support:,.2f}，距離 {d:.1f}%，跌破就該檢討")
        else:
            add("warn", "支撐", f"下方支撐在 {support:,.2f}，距離 {d:.0f}% 偏遠，回檔可能比較深")
    else:
        add("info", "支撐", "下方找不到明確支撐")

    # 4. 壓力
    if resistance is None:
        add("good", "壓力", "上方沒有前波高點壓著，賣壓相對輕")
    else:
        d = (resistance / close - 1) * 100
        if d <= 3:
            add("warn", "壓力", f"離前高壓力 {resistance:,.2f} 只剩 {d:.1f}%，要看能不能帶量突破")
        else:
            add("info", "壓力", f"上方壓力在 {resistance:,.2f}，還有 {d:.0f}% 空間")

    # 5. 量價（近 10 天）
    d10 = df.iloc[-11:]
    chg10 = d10["Close"].diff().iloc[1:]
    vol10 = d10["Volume"].iloc[1:]
    up_v, dn_v = vol10[chg10 > 0].mean(), vol10[chg10 < 0].mean()
    if up_v == up_v and dn_v == dn_v and dn_v > 0:
        ratio = up_v / dn_v
        if ratio >= 1.2:
            add("good", "量價", "近 10 天上漲有量、下跌量縮，量價健康")
        elif ratio <= 0.8:
            add("bad", "量價", "近 10 天下跌時量比較大，留意有人在出貨")
        else:
            add("info", "量價", "近 10 天量價沒有明顯偏向")

    # 6. 籌碼（只有上市有資料）
    if chips_ok and hist and any(d and code in d for d in hist):
        t_buy = streak(chip_series(hist, code, "trust"))
        t_sell = streak(chip_series(hist, code, "trust"), positive=False)
        all_sell = streak(chip_series(hist, code, "total"), positive=False)
        if all_sell >= 3:
            add("bad", "籌碼", f"三大法人連賣 {all_sell} 天")
        elif t_sell >= 3:
            add("bad", "籌碼", f"投信連賣 {t_sell} 天")
        elif t_buy >= 3:
            add("good", "籌碼", f"投信連買 {t_buy} 天")
        else:
            add("info", "籌碼", "法人最近沒有明顯方向")
    else:
        add("info", "籌碼", "這檔沒有法人資料（上櫃股暫不支援）")

    # 7. 乖離、回落
    bias = (close / m20 - 1) * 100
    if bias > 15:
        add("warn", "乖離", f"離月線 {bias:.0f}%，漲多容易回檔")
    drop = (close / df["High"].iloc[-20:].max() - 1) * 100
    if drop < -10:
        add("warn", "回落", f"從近 20 天高點回落 {abs(drop):.0f}%")

    bad = sum(x["s"] == "bad" for x in checks)
    warn = sum(x["s"] == "warn" for x in checks)
    if (close < m20 and not rising) or bad >= 2:
        verdict, summary = "exit", "趨勢已經轉弱，考慮減碼或出場"
    elif bad == 1 or warn >= 3:
        verdict, summary = "watch", "還沒壞，但有警訊，抱著要設好停損"
    else:
        verdict, summary = "hold", "趨勢健康，可以續抱"

    tail = df.iloc[-60:]
    return {
        "date": str(df.index[-1].date()), "close": r2(close),
        "chg": r2((close / prev - 1) * 100),
        "ma20": r2(m20), "support": r2(support), "resistance": r2(resistance),
        "closes": [r2(x) for x in tail["Close"]],
        "ma20s": [r2(x) for x in ma20.iloc[-60:]],
        "checks": checks, "verdict": verdict, "summary": summary,
    }


def build_site(day, names, prices, hist, chips_ok, picks, pullbacks):
    """產生網頁資料 site/data.json，網頁本身是 site/index.html"""
    stocks = {}
    for code, df in prices.items():
        if len(df) < 25:
            continue
        try:
            h = health_check(df, hist, code, chips_ok)
            h["name"] = names.get(code, "")
            stocks[code] = h
        except Exception as e:
            print(code, "健檢失敗：", e)

    def brief(code, name, r):
        return {"code": code, "name": name, "score": r["score"], "tags": r["tags"],
                "close": r2(r["close"]), "chg": r2(r["chg"]),
                "support": r2(r["support"]), "resistance": r2(r["resistance"]),
                "notes": r["notes"], "hot": r.get("consolidation", False)}

    data = {"date": str(day),
            "updated": dt.datetime.now(TW).strftime("%Y-%m-%d %H:%M"),
            "stocks": stocks,
            "picks": [brief(*p) for p in picks],
            "pullbacks": [brief(*p) for p in pullbacks]}
    os.makedirs("site", exist_ok=True)
    with open("site/data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    if os.path.exists("index.html"):
        with open("index.html", encoding="utf-8") as src_f, \
             open("site/index.html", "w", encoding="utf-8") as dst_f:
            dst_f.write(src_f.read())
    print(f"網站資料：{len(stocks)} 檔")


# ---------------- 訊息 ----------------
def fmt_levels(r):
    s = f"{r['support']:,.2f}" if r["support"] else "—"
    p = f"{r['resistance']:,.2f}" if r["resistance"] else "無"
    return f"📐 支撐 {s}｜壓力 {p}"


def build_message(day, holdings, picks, total, pullbacks, chips_ok):
    L = [f"📊 {day:%m/%d} 盤後報告", ""]

    if holdings:
        L.append("📌 持股檢查")
        for code, name, h in holdings:
            if h is None:
                L.append(f"{code} 抓不到資料"); continue
            L.append(f"{code} {name} 收 {h['close']:,.2f}（{h['chg']:+.1f}%）離月線 {h['bias']:+.0f}%")
            L.append(fmt_levels(h))
            L.append("🚨 " + "、".join(h["alerts"]) if h["alerts"] else "✅ 趨勢還在")
        L.append("")

    L.append(f"🚀 突破型：符合 {total} 檔，前 {len(picks)} 名")
    L.append("")
    if not picks:
        L.append("今天沒有符合條件的股票，休息也是一種操作 ☕")
    for n, (code, name, r) in enumerate(picks, 1):
        mark = "🔥" if r["consolidation"] else ""
        L.append(f"{n}. {mark}{code} {name}｜{r['score']}分")
        L.append(f"收 {r['close']:,.2f}（{r['chg']:+.1f}%）　月線 {r['ma20']:,.2f}")
        L.append("✅ " + "、".join(r["tags"]))
        L.append(fmt_levels(r))
        L.append("⏱ " + "、".join(r["notes"]))
        if r["warns"]:
            L.append("⚠️ " + "、".join(r["warns"]))
        L.append("")

    L.append(f"🔄 拉回支撐型：{len(pullbacks)} 檔")
    L.append("")
    if not pullbacks:
        L.append("今天沒有拉回到支撐的強勢股")
        L.append("")
    for n, (code, name, r) in enumerate(pullbacks, 1):
        L.append(f"{n}. {code} {name}｜{r['score']}分")
        L.append(f"收 {r['close']:,.2f}（{r['chg']:+.1f}%）")
        L.append("✅ " + "、".join(r["tags"]))
        L.append(fmt_levels(r))
        L.append("⏱ " + "、".join(r["notes"]))
        L.append("")
    if not chips_ok:
        L.append("（今天法人資料沒抓到，籌碼分數未計入）")
    L.append("🔥＝盤整突破｜跌破支撐＝出場參考")
    if os.getenv("SITE_URL"):
        L.append("📱 個股健檢：" + os.getenv("SITE_URL"))
    L.append("※ 條件篩選結果，不是買賣建議")
    return "\n".join(L)[:4900]


def send_line(text):
    token, uid = os.getenv("LINE_TOKEN"), os.getenv("LINE_USER_ID")
    if not token or not uid:
        print(text); return
    r = requests.post("https://api.line.me/v2/bot/message/push",
                      headers={"Authorization": f"Bearer {token}"},
                      json={"to": uid, "messages": [{"type": "text", "text": text}]},
                      timeout=30)
    print("LINE 回應：", r.status_code, r.text)


# ---------------- 主程式 ----------------
def main():
    names, candidates = get_universe()
    holding_codes = read_holdings()
    print(f"股票池：{len(candidates)} 檔，持股 {len(holding_codes)} 檔")
    prices = get_prices(candidates)
    if "2330" not in prices:
        sys.exit("抓不到股價資料，結束")
    for code in holding_codes:          # 上櫃持股另外抓
        if code not in prices:
            df = get_single_price(code)
            if df is not None:
                prices[code] = df

    trade_days = [d.date() for d in prices["2330"].index]
    last_day = trade_days[-1]
    is_new_day = last_day == dt.datetime.now(TW).date()

    hist = get_chip_history(trade_days[-CHIP_DAYS:])
    today_chips = hist[-1]
    chips_ok = today_chips is not None

    results, pullbacks = [], []
    for code, df in prices.items():
        if code not in names or df.index[-1].date() != last_day:
            continue
        chip = (today_chips or {}).get(code)
        t_streak = streak(chip_series(hist, code, "trust"))
        r = analyze(df, chip, t_streak)
        if r and r["score"] >= MIN_SCORE and (r["consolidation"] or not ONLY_CONSOLIDATION):
            results.append((code, names[code], r))
        p = analyze_pullback(df, chip, t_streak)
        if p:
            pullbacks.append((code, names[code], p))
    results.sort(key=lambda x: (x[2]["score"], x[2]["vr"]), reverse=True)
    pullbacks.sort(key=lambda x: (x[2]["score"], -x[2]["vr"]), reverse=True)
    picks, pulls = results[:TOP_N], pullbacks[:PULLBACK_TOP_N]

    holdings = []
    for code in holding_codes:
        df = prices.get(code)
        h = check_holding(df, hist, code) if df is not None and len(df) >= 20 else None
        holdings.append((code, names.get(code, ""), h))

    build_site(last_day, names, prices, hist, chips_ok, picks, pulls)

    if not is_new_day and os.getenv("FORCE") != "true":
        print("今天沒有新資料（可能休市），只更新網站，不推 LINE")
        return
    send_line(build_message(last_day, holdings, picks, len(results), pulls, chips_ok))


if __name__ == "__main__":
    main()
