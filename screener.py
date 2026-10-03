# -*- coding: utf-8 -*-
"""
秀珊的台股盤後選股系統（只做多，抱 1～3 個月）

每天收盤後：
  1. 資金輪動：哪些產業、題材今天有資金流入／流出
  2. 三份名單：🔥 糾結突破、🚀 一般突破、🔄 拉回支撐
     每檔附：技術／籌碼／基本／消息四面結論、參考進場區、停損、30 天停利
  3. 持股健檢（妳自己的持股推 LINE，其他人用 LINE 輸入「持股」查）
  4. 產生網頁與 LINE 機器人要用的資料
"""
import os, sys, csv, io, time, json, datetime as dt
import xml.etree.ElementTree as ET
from urllib.parse import quote
import requests
import pandas as pd
import yfinance as yf

# ================== 可以自己調整的條件 ==================
MIN_AVG_VOLUME_LOTS = 1000   # 20 日平均成交量至少幾張
MIN_PRICE, MAX_PRICE = 10, 2000
BOX_MAX_RANGE = 15           # 盤整箱型：最高最低差幾 % 以內
MA_TANGLE = 3.0              # 均線糾結：5、10、20 日線差距幾 % 以內
TANGLE_DAYS = 5              # 突破前至少連續幾天糾結
BREAKOUT_VOL = 2.0           # 糾結／箱型突破至少幾倍量
GENERAL_VOL = 1.5            # 一般突破至少幾倍量
BREAKOUT_LOOKBACK = 3        # 突破後幾天內都算「剛突破」
BREAKOUT_MAX_ABOVE = 5       # 收盤離突破點最多高幾 %（超過算已經噴走）
NOT_HIGH_TANGLE = 35         # 突破點離半年低點最多漲幾 %
NOT_HIGH_GENERAL = 50
MAX_BIAS = 10                # 離月線最多幾 %
RSI_HOT = 80                 # RSI 超過算過熱，不列入
LIST_N = 5                   # 每份名單最多幾檔
MTF_MIN_SCORE = 50           # 日/週/月多週期技術分數最低門檻（100分）
TREND_VOL = 1.2              # 非突破型趨勢轉強，至少要有 1.2 倍量
STRONG_VOL = 1.5             # 強勢續攻近期至少要有明顯放量
RR_FORMAL_MIN = 1.5          # 正式做多名單最低報酬風險比
RR_WATCH_MIN = 1.0           # 低於 1.0 不列推薦；1.0~1.49 列觀察
CHIP_DAYS = 10
THEMES_FILE = "themes.csv"
# =======================================================

TW = dt.timezone(dt.timedelta(hours=8))
HEADERS = {"User-Agent": "Mozilla/5.0"}
SITE_URL = os.getenv("SITE_URL", "")
MARKET_SUFFIX = {}   # 代號 -> .TW（上市）或 .TWO（上櫃）


# ---------------- 小工具 ----------------
def to_num(x):
    try:
        return float(str(x).replace(",", "").strip())
    except Exception:
        return None


def r2(x):
    try:
        x = float(x)
        return None if x != x else round(x, 2)
    except Exception:
        return None


def pick(row, inc, exc=()):
    for k, v in row.items():
        if all(s in k for s in inc) and not any(s in k for s in exc):
            return v
    return None


def get_json(url):
    try:
        return requests.get(url, headers=HEADERS, timeout=40).json()
    except Exception as e:
        print("抓取失敗：", url, e)
        return []


def streak(vals, positive=True):
    n = 0
    for x in reversed(vals):
        if (x > 0) if positive else (x < 0):
            n += 1
        else:
            break
    return n


def item(s, t, d):
    return {"s": s, "t": t, "d": d}


def verdict_of(items):
    g = sum(i["s"] == "good" for i in items)
    b = sum(i["s"] == "bad" for i in items)
    w = sum(i["s"] == "warn" for i in items)
    if b or w >= 2:
        return "bad"
    return "good" if g >= 2 else "neutral"


def owner_chip_line(d):
    parts = []
    if d.get("big_pct") is not None:
        parts.append(f"大戶 {d['big_pct']:.1f}%")
    chip = d.get("chip_today") or {}
    if chip:
        parts.append(f"外資 {chip.get('foreign', 0):+,.0f}張")
        parts.append(f"投信 {chip.get('trust', 0):+,.0f}張")
        parts.append(f"自營商 {chip.get('dealer', 0):+,.0f}張")
    return "👥 " + "｜".join(parts) if parts else ""


# ---------------- 抓資料 ----------------
def get_universe():
    """建立上市＋上櫃選股池。"""
    names = {}
    MARKET_SUFFIX.clear()

    # 上市
    for row in get_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"):
        code = str(row.get("Code", "")).strip()
        if len(code) == 4 and code.isdigit() and not code.startswith("0"):
            names[code] = row.get("Name", "")
            MARKET_SUFFIX[code] = ".TW"

    # 上櫃：櫃買中心每日收盤行情
    try:
        today = dt.datetime.now(TW).date()
        roc = f"{today.year - 1911}/{today.month:02d}/{today.day:02d}"
        url = ("https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/"
               f"stk_quote_result.php?l=zh-tw&o=json&d={roc}")
        j = requests.get(url, headers=HEADERS, timeout=40).json()
        tables = j.get("tables") or []
        for table in tables:
            fields = table.get("fields") or []
            data = table.get("data") or []
            if not fields or not data:
                continue
            try:
                i_code = next(i for i, x in enumerate(fields) if "代號" in str(x))
                i_name = next(i for i, x in enumerate(fields) if "名稱" in str(x))
            except StopIteration:
                continue
            for row in data:
                try:
                    code = str(row[i_code]).strip()
                    name = str(row[i_name]).strip()
                    if len(code) == 4 and code.isdigit() and not code.startswith("0"):
                        names[code] = name
                        MARKET_SUFFIX[code] = ".TWO"
                except Exception:
                    continue
    except Exception as e:
        print("上櫃股票清單抓取失敗：", e)

    print(f"選股池：上市 {sum(v == '.TW' for v in MARKET_SUFFIX.values())} 檔、上櫃 {sum(v == '.TWO' for v in MARKET_SUFFIX.values())} 檔")
    return names


def get_twse_latest_ohlcv(target_day):
    """依指定日期取得證交所全部上市股票 OHLCV，不使用『最新資料』端點。"""
    date_str = target_day.strftime("%Y%m%d")
    url = f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={date_str}&type=ALLBUT0999&response=json"
    try:
        j = requests.get(url, headers=HEADERS, timeout=40).json()
    except Exception as e:
        print(f"{target_day} 指定日期行情抓取失敗：", e)
        return {}

    if str(j.get("stat", "")).upper() not in ("OK", ""):
        print(f"{target_day} 證交所行情狀態異常：", j.get("stat"))
        return {}

    candidates = []
    if isinstance(j.get("tables"), list):
        candidates.extend(j["tables"])

    for n in range(1, 20):
        f = j.get(f"fields{n}")
        d = j.get(f"data{n}")
        if isinstance(f, list) and isinstance(d, list):
            candidates.append({"fields": f, "data": d})

    wanted = None
    for table in candidates:
        fields = table.get("fields") or []
        data = table.get("data") or []
        if not fields or not data:
            continue
        joined = "|".join(str(x) for x in fields)
        if "證券代號" in joined and "開盤價" in joined and "最高價" in joined and "最低價" in joined and "收盤價" in joined:
            wanted = (fields, data)
            break

    if not wanted:
        print(f"{target_day} 找不到證交所個股 OHLC 表格")
        return {}

    fields, data = wanted

    def col(name):
        for i, x in enumerate(fields):
            if name in str(x):
                return i
        return None

    i_code = col("證券代號")
    i_open = col("開盤價")
    i_high = col("最高價")
    i_low = col("最低價")
    i_close = col("收盤價")
    i_vol = col("成交股數")
    if None in (i_code, i_open, i_high, i_low, i_close, i_vol):
        print(f"{target_day} 證交所欄位不完整：", fields)
        return {}

    out = {}
    for row in data:
        try:
            code = str(row[i_code]).strip()
            if len(code) != 4 or not code.isdigit() or code.startswith("0"):
                continue
            o = to_num(row[i_open]); h = to_num(row[i_high]); l = to_num(row[i_low])
            c = to_num(row[i_close]); v = to_num(row[i_vol])
            if None in (o, h, l, c, v):
                continue
            out[code] = {"Open": o, "High": h, "Low": l, "Close": c, "Volume": v}
        except Exception:
            continue

    print(f"證交所指定日期 {target_day} 行情：{len(out)} 檔")
    if "2330" in out:
        print(f"證交所 2330 {target_day} 收盤：{out['2330']['Close']}")
    return out


def patch_latest_twse_day(prices, target_day):
    """Yahoo 少最新交易日時，用『指定日期』證交所官方 OHLCV 補最後一天。"""
    if not target_day:
        return 0
    official = get_twse_latest_ohlcv(target_day)
    if not official:
        print(f"⚠️ {target_day} 官方指定日期行情抓不到，不補日期、不推播")
        return 0

    ts = pd.Timestamp(target_day)
    patched = 0
    for code, row in official.items():
        df = prices.get(code)
        if df is None or len(df) == 0:
            continue
        try:
            last = pd.Timestamp(df.index[-1]).date()
            if last >= target_day:
                continue
            prices[code] = pd.concat([df, pd.DataFrame([row], index=[ts])])
            patched += 1
        except Exception as e:
            print(code, "補最新行情失敗：", e)
    print(f"已真正補上 {target_day} 官方行情：{patched} 檔")
    return patched


def get_prices(codes, target_day=None):
    result = {}
    for suffix in (".TW", ".TWO"):
        market_codes = [c for c in codes if MARKET_SUFFIX.get(c, ".TW") == suffix]
        tickers = [c + suffix for c in market_codes]
        for i in range(0, len(tickers), 100):
            batch = tickers[i:i + 100]
            if not batch:
                continue
            data = yf.download(batch, period="9mo", interval="1d", group_by="ticker",
                               auto_adjust=False, threads=True, progress=False)
            for t in batch:
                try:
                    df = data[t][["Open", "High", "Low", "Close", "Volume"]].dropna()
                    if len(df):
                        code = t.replace(".TW", "").replace(".TWO", "")
                        result[code] = df
                except Exception:
                    pass
            time.sleep(2)
    return result


def get_single_price(code):
    preferred = MARKET_SUFFIX.get(code)
    suffixes = [preferred] if preferred else []
    suffixes += [s for s in (".TW", ".TWO") if s not in suffixes]
    for suffix in suffixes:
        try:
            df = yf.Ticker(code + suffix).history(period="9mo", auto_adjust=False)
            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
            if len(df):
                df.index = df.index.tz_localize(None)
                return df
        except Exception:
            pass
    return None


def get_latest_twse_trade_day(max_lookback=10):
    """從證交所指定日期資料找最近真正的交易日；週末/休市日會自動往前找。"""
    today = dt.datetime.now(TW).date()
    for n in range(max_lookback):
        day = today - dt.timedelta(days=n)
        if day.weekday() >= 5:
            continue
        try:
            url = f"https://www.twse.com.tw/rwd/zh/fund/T86?date={day:%Y%m%d}&selectType=ALLBUT0999&response=json"
            j = requests.get(url, headers=HEADERS, timeout=30).json()
            if j.get("data"):
                return day
        except Exception as e:
            print(f"{day} 交易日確認失敗：", e)
    return None


def get_chips_one(date_str):
    url = f"https://www.twse.com.tw/rwd/zh/fund/T86?date={date_str}&selectType=ALLBUT0999&response=json"
    try:
        j = requests.get(url, headers=HEADERS, timeout=30).json()
        f = j["fields"]
        i_code = f.index("證券代號")
        i_for = next(i for i, x in enumerate(f) if x.startswith("外陸資買賣超"))
        i_tru = next(i for i, x in enumerate(f) if x.startswith("投信買賣超"))
        i_tot = next(i for i, x in enumerate(f) if x.startswith("三大法人買賣超"))
        out = {}
        for row in j["data"]:
            foreign = to_num(row[i_for]) or 0
            trust = to_num(row[i_tru]) or 0
            total = to_num(row[i_tot]) or 0
            out[row[i_code].strip()] = {
                "foreign": foreign / 1000,
                "trust": trust / 1000,
                "dealer": (total - foreign - trust) / 1000,
                "total": total / 1000,
            }
        return out
    except Exception as e:
        print(f"{date_str} 法人資料抓取失敗：", e)
        return None


def get_chip_history(dates):
    hist = []
    for d in dates:
        hist.append(get_chips_one(d.strftime("%Y%m%d")))
        time.sleep(3)
    return hist


def chip_series(hist, code, key):
    return [(d.get(code) or {}).get(key, 0) for d in hist if d is not None]


def get_fundamentals():
    F = {}
    for row in get_json("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"):
        code = str(row.get("Code") or pick(row, ["代號"]) or "").strip()
        if not code:
            continue
        pe = to_num(row.get("PEratio") or pick(row, ["本益比"]))
        F.setdefault(code, {}).update({
            "pe": pe if pe and pe > 0 else None,
            "yield": to_num(row.get("DividendYield") or pick(row, ["殖利率"])),
            "pb": to_num(row.get("PBratio") or pick(row, ["淨值比"])),
            "has_pe": True})
    for row in get_json("https://openapi.twse.com.tw/v1/opendata/t187ap05_L"):
        code = str(pick(row, ["公司代號"]) or "").strip()
        if code:
            F.setdefault(code, {}).update({
                "industry": (pick(row, ["產業別"]) or "").strip(),
                "rev_ym": str(pick(row, ["資料年月"]) or "").strip(),
                "rev_yoy": to_num(pick(row, ["去年同月增減"])),
                "rev_cum": to_num(pick(row, ["前期比較增減"]))})
    for row in get_json("https://openapi.twse.com.tw/v1/opendata/t187ap04_L"):
        code = str(pick(row, ["公司代號"]) or "").strip()
        subj = (pick(row, ["主旨"]) or "").strip()
        if code and subj:
            F.setdefault(code, {}).setdefault("notices", []).append(
                {"date": str(pick(row, ["發言日期"]) or ""), "title": subj[:80]})
    groups = {}
    for f in F.values():
        if f.get("pe") and f.get("industry"):
            groups.setdefault(f["industry"], []).append(f["pe"])
    med = {k: sorted(v)[len(v) // 2] for k, v in groups.items() if len(v) >= 5}
    for f in F.values():
        f["ind_pe"] = med.get(f.get("industry"))
    print(f"基本面：{len(F)} 檔")
    return F


def get_margin():
    """融資融券餘額（張）。證交所晚上才更新，下午抓到的是前一交易日"""
    out = {}
    for row in get_json("https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN"):
        code = str(pick(row, ["代號"]) or row.get("Code") or "").strip()
        fin = to_num(pick(row, ["融資", "今日"]))
        sh = to_num(pick(row, ["融券", "今日"]))
        if code and fin is not None:
            out[code] = (fin, sh or 0)
    print(f"融資融券：{len(out)} 檔")
    return out


def get_big_holders():
    """集保 400 張以上大戶持股比例（每週更新）"""
    for url in ("https://opendata.tdcc.com.tw/getOD.ashx?id=1-5",
                "https://smart.tdcc.com.tw/opendata/getOD.ashx?id=1-5"):
        try:
            text = requests.get(url, headers=HEADERS, timeout=90).content.decode("utf-8-sig")
            rows = list(csv.reader(io.StringIO(text)))
            head = rows[0]
            i_date = next(i for i, h in enumerate(head) if "日期" in h)
            i_code = next(i for i, h in enumerate(head) if "代號" in h)
            i_lv = next(i for i, h in enumerate(head) if "分級" in h)
            i_pct = next(i for i, h in enumerate(head) if "比例" in h)
            big, date = {}, rows[1][i_date].strip()
            for r in rows[1:]:
                if len(r) > i_pct and r[i_lv].strip() in ("12", "13", "14", "15"):
                    code = r[i_code].strip()
                    big[code] = big.get(code, 0) + (to_num(r[i_pct]) or 0)
            print(f"集保大戶：{len(big)} 檔，資料日 {date}")
            return date, big
        except Exception as e:
            print("集保資料抓取失敗：", url, e)
    return None, {}


POS_WORDS = ["創新高", "大漲", "漲停", "調升", "上修", "利多", "成長", "創高", "接單", "擴產",
             "買超", "看好", "上調", "強勢", "樂觀", "轉盈", "爆發", "旺", "大單", "噴"]
NEG_WORDS = ["大跌", "跌停", "下修", "調降", "利空", "衰退", "虧損", "裁員", "賣超", "砍單",
             "減產", "警示", "處置", "違約", "下滑", "疲弱", "保守", "罰", "重挫", "崩"]
NOTICE_NEG = ["虧損", "減資", "違約", "跳票", "重整", "停工", "下修", "衰退", "裁員", "訴訟",
              "罰", "處分", "解任", "掏空", "財務困難", "暫停交易", "變更交易", "全額交割", "退票"]


def tag_title(t):
    p = sum(w in t for w in POS_WORDS)
    n = sum(w in t for w in NEG_WORDS)
    return "pos" if p > n else "neg" if n > p else "neu"


def get_news(code, name, n=5):
    url = (f"https://news.google.com/rss/search?q={quote(name + ' ' + code)}+when:7d"
           "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
    try:
        root = ET.fromstring(requests.get(url, headers=HEADERS, timeout=20).content)
        items = []
        for it in root.iter("item"):
            title, src = it.findtext("title") or "", it.findtext("source") or ""
            if src and title.endswith(" - " + src):
                title = title[: -len(src) - 3]
            items.append({"title": title, "link": it.findtext("link") or "", "source": src,
                          "date": (it.findtext("pubDate") or "")[5:16], "tag": tag_title(title)})
            if len(items) >= n:
                break
        return items
    except Exception as e:
        print(code, "新聞抓取失敗：", e)
        return []


def load_themes():
    themes = {}
    if not os.path.exists(THEMES_FILE):
        return themes
    for line in open(THEMES_FILE, encoding="utf-8"):
        line = line.split("#")[0].strip()
        if ":" not in line and "：" not in line:
            continue
        name, codes = line.replace("：", ":").split(":", 1)
        for c in codes.replace(",", " ").split():
            themes.setdefault(c.strip(), []).append(name.strip())
    return themes


def get_holdings():
    owner = [c.strip() for c in os.getenv("HOLDINGS", "").replace("，", ",").replace(" ", ",").split(",") if c.strip()]
    everyone = list(owner)
    url, key = os.getenv("WORKER_URL"), os.getenv("SYNC_KEY")
    if url and key:
        try:
            j = requests.get(url.rstrip("/") + "/holdings", params={"key": key}, timeout=20).json()
            owner += j.get("owner", [])
            everyone += j.get("all", [])
        except Exception as e:
            print("讀取 LINE 持股失敗：", e)
    return list(dict.fromkeys(owner)), list(dict.fromkeys(everyone))


def load_prev_hist():
    if not SITE_URL:
        return {"big": {}, "margin": {}}
    try:
        return requests.get(SITE_URL + "hist.json", timeout=30).json()
    except Exception:
        return {"big": {}, "margin": {}}


# ---------------- 技術指標 ----------------
def indicators(df):
    c, v = df["Close"], df["Volume"]
    I = pd.DataFrame(index=df.index)
    for n in (5, 10, 20, 60):
        I[f"ma{n}"] = c.rolling(n).mean()
    I["vma20"] = v.rolling(20).mean()
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    I["rsi"] = 100 - 100 / (1 + up / dn.replace(0, 1e-9))
    I["dif"] = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    I["dea"] = I["dif"].ewm(span=9, adjust=False).mean()
    I["hist"] = I["dif"] - I["dea"]
    return I


def macd_state(c, I):
    dif, dea, h = I["dif"].values, I["dea"].values, I["hist"].values
    n = len(dif)
    s = {"golden": False, "zero": False, "dead": False, "rising": False, "div": False}
    for k in range(1, 6):
        i = n - k
        if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            s["golden"] = True
            s["zero"] = abs(dif[i]) / c.iloc[i] <= 0.015
            break
    for k in range(1, 4):
        i = n - k
        if dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
            s["dead"] = True
            break
    s["rising"] = bool(h[-1] > h[-2] > h[-3] and h[-1] > 0)
    if n >= 60:
        cl = c.values
        if cl[-10:].max() >= cl[-60:].max() and cl[-10:].max() > cl[-60:-10].max() \
                and dif[-10:].max() < dif[-60:-10].max():
            s["div"] = True
    return s


def pivots(df, n=5, lookback=120):
    d = df.iloc[-lookback:]
    H, L = d["High"].values, d["Low"].values
    highs, lows = [], []
    for i in range(n, len(d) - n):
        if H[i] == H[i - n:i + n + 1].max():
            highs.append(float(H[i]))
        if L[i] == L[i - n:i + n + 1].min():
            lows.append(float(L[i]))
    return highs, lows


def find_levels(df, ma20):
    close = float(df["Close"].iloc[-1])
    highs, lows = pivots(df)
    above = [h for h in highs if h > close * 1.01]
    below = [l for l in lows if l < close] + [h for h in highs if h < close * 0.99]
    if ma20 < close:
        below.append(ma20)
    return (max(below) if below else None), (min(above) if above else None)


def resample_ohlcv(df, rule):
    return df.resample(rule).agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"
    }).dropna()


def timeframe_score(df, I, ms):
    close = float(df["Close"].iloc[-1])
    rsi = float(I["rsi"].iloc[-1])
    dscore = 0
    if close > I["ma20"].iloc[-1]: dscore += 10
    if I["ma20"].iloc[-1] > I["ma60"].iloc[-1]: dscore += 8
    if I["ma20"].iloc[-1] > I["ma20"].iloc[-6]: dscore += 8
    if I["ma5"].iloc[-1] > I["ma10"].iloc[-1] > I["ma20"].iloc[-1]: dscore += 6
    if ms["zero"]: dscore += 10
    elif ms["golden"]: dscore += 7
    if ms["rising"]: dscore += 5
    if 50 <= rsi <= 70: dscore += 7
    elif 45 <= rsi <= 75: dscore += 4
    vr_now = float(df["Volume"].iloc[-1] / max(I["vma20"].iloc[-2], 1))
    if vr_now >= 1.5: dscore += 6
    elif vr_now >= 1.1: dscore += 3
    dscore = min(60, dscore)

    wscore = 0
    try:
        w = resample_ohlcv(df, "W-FRI")
        if len(w) >= 14:
            wc = w["Close"]
            wma4 = wc.rolling(4).mean(); wma12 = wc.rolling(12).mean()
            wd = wc.ewm(span=12, adjust=False).mean() - wc.ewm(span=26, adjust=False).mean()
            wdea = wd.ewm(span=9, adjust=False).mean(); wh = wd - wdea
            if wc.iloc[-1] > wma4.iloc[-1]: wscore += 8
            if wma4.iloc[-1] > wma12.iloc[-1]: wscore += 8
            if wma4.iloc[-1] > wma4.iloc[-3]: wscore += 6
            if wh.iloc[-1] > 0: wscore += 5
            if len(wh) >= 3 and wh.iloc[-1] > wh.iloc[-2]: wscore += 3
    except Exception:
        pass
    wscore = min(30, wscore)

    mscore = 0
    try:
        m = resample_ohlcv(df, "ME")
        if len(m) >= 4:
            mc = m["Close"]; mma3 = mc.rolling(3).mean()
            if mc.iloc[-1] > mma3.iloc[-1]: mscore += 5
            if mma3.iloc[-1] > mma3.iloc[-2]: mscore += 5
    except Exception:
        try:
            m = resample_ohlcv(df, "M")
            if len(m) >= 4:
                mc = m["Close"]; mma3 = mc.rolling(3).mean()
                if mc.iloc[-1] > mma3.iloc[-1]: mscore += 5
                if mma3.iloc[-1] > mma3.iloc[-2]: mscore += 5
        except Exception:
            pass

    total = int(dscore + wscore + mscore)
    if dscore >= 42 and wscore >= 20: label = "🔥🌊 短中波段皆可"
    elif wscore >= 22 and mscore >= 5: label = "🌊 大波段潛力"
    elif dscore >= 42: label = "🔥 短波段轉強"
    else: label = "📈 偏多觀察"
    return {"total": total, "daily": int(dscore), "weekly": int(wscore),
            "monthly": int(mscore), "label": label, "vr": r2(vr_now)}


def detect_trend_setup(df, I, support):
    close = float(df["Close"].iloc[-1])
    ma20 = float(I["ma20"].iloc[-1]); ma60 = float(I["ma60"].iloc[-1])
    rsi = float(I["rsi"].iloc[-1]); vr = float(df["Volume"].iloc[-1] / max(I["vma20"].iloc[-2], 1))
    bias = (close / ma20 - 1) * 100
    if not (close > ma20 and ma20 >= ma60 * 0.98): return None
    if not (I["ma20"].iloc[-1] > I["ma20"].iloc[-6]): return None
    if not (48 <= rsi <= 72) or bias > 8 or vr < TREND_VOL: return None
    if close <= df["Close"].iloc[-6:-1].mean(): return None
    return {"kind": "trend", "vr": vr, "day": 1,
            "days": 20, "top": close, "bot": float(support or ma20), "rng": r2(bias)}


def detect_strong_continuation(df, I, support):
    if len(df) < 65: return None
    c = df["Close"]; close = float(c.iloc[-1]); ma20 = float(I["ma20"].iloc[-1]); ma60 = float(I["ma60"].iloc[-1])
    rsi = float(I["rsi"].iloc[-1]); bias = (close / ma20 - 1) * 100; ret3 = (close / float(c.iloc[-4]) - 1) * 100
    hi20_prev = float(df["High"].iloc[-21:-1].max()); near_high = close >= hi20_prev * 0.97
    recent_vr = 0.0
    for i in range(-3, 0):
        base = float(I["vma20"].iloc[i - 1]) if I["vma20"].iloc[i - 1] == I["vma20"].iloc[i - 1] else 0
        if base > 0: recent_vr = max(recent_vr, float(df["Volume"].iloc[i] / base))
    if not (close > ma20 > ma60): return None
    if not (I["ma20"].iloc[-1] > I["ma20"].iloc[-6]): return None
    if not near_high or ret3 < 4: return None
    if recent_vr < STRONG_VOL: return None
    if not (58 <= rsi <= 85): return None
    if bias > 18: return None
    return {"kind": "strong", "vr": recent_vr, "day": 1, "days": 20, "top": hi20_prev,
            "bot": float(support or ma20), "rng": r2(bias), "ret3": r2(ret3)}


def find_box(df, e):
    for days in (60, 40, 20):
        if e - days < 0: continue
        w = df.iloc[e - days:e]
        top, bot = float(w["High"].max()), float(w["Low"].min())
        rng = (top / bot - 1) * 100
        if rng <= BOX_MAX_RANGE: return {"days": days, "top": top, "bot": bot, "rng": rng}
    return None


def tangled(I, e):
    for i in range(e - TANGLE_DAYS, e):
        m = [I["ma5"].iloc[i], I["ma10"].iloc[i], I["ma20"].iloc[i]]
        if min(m) <= 0 or (max(m) / min(m) - 1) * 100 > MA_TANGLE: return False
    return True


def detect_breakout(df, I):
    n = len(df); c, v, vma = df["Close"].values, df["Volume"].values, I["vma20"].values
    for k in range(BREAKOUT_LOOKBACK):
        e = n - 1 - k; box = find_box(df, e)
        if box and c[e] > box["top"] >= c[e - 1] and v[e] / vma[e - 1] >= BREAKOUT_VOL:
            if not (box["top"] * 0.99 <= c[-1] <= box["top"] * (1 + BREAKOUT_MAX_ABOVE / 100)): return None
            kind = "tangle" if tangled(I, e) else "box"
            return {"kind": kind, "day": k + 1, "vr": v[e] / vma[e - 1], **box}
    for k in range(BREAKOUT_LOOKBACK):
        e = n - 1 - k; hi20 = float(df["High"].iloc[e - 20:e].max())
        if c[e] > hi20 >= c[e - 1] and v[e] / vma[e - 1] >= GENERAL_VOL:
            if not (hi20 * 0.99 <= c[-1] <= hi20 * (1 + BREAKOUT_MAX_ABOVE / 100)): return None
            lo20 = float(df["Low"].iloc[e - 20:e].min())
            return {"kind": "high20", "day": k + 1, "vr": v[e] / vma[e - 1], "days": 20,
                    "top": hi20, "bot": lo20, "rng": (hi20 / lo20 - 1) * 100}
    return None


def detect_pullback(df, I, support):
    c = df["Close"]; close, op, hi, lo = c.iloc[-1], df["Open"].iloc[-1], df["High"].iloc[-1], df["Low"].iloc[-1]
    ma20, ma60 = I["ma20"], I["ma60"]
    if not (ma60.iloc[-1] > ma60.iloc[-6] and ma20.iloc[-1] > ma60.iloc[-1] and close > ma60.iloc[-1]): return None
    if close > df["High"].iloc[-10:].max() * 0.95 or not support: return None
    dist = (close / support - 1) * 100; rsi = I["rsi"].iloc[-1]; vr = df["Volume"].iloc[-1] / I["vma20"].iloc[-2]
    if not (0 <= dist <= 3) or vr > 1.0 or not (38 <= rsi <= 55): return None
    stop_k = close > op or (hi > lo and (min(op, close) - lo) / (hi - lo) >= 0.4)
    if not stop_k: return None
    return {"kind": "pullback", "vr": vr, "dist": dist}


def tech_items_pick(pat, I, ms, rsi):
    it, pts = [], 0; k = pat["kind"]
    if k == "tangle":
        it.append(item("good", "型態", f"均線糾結後帶量突破，盤整 {pat['days']} 天（{pat['bot']:,.2f}～{pat['top']:,.2f}）")); pts += 4 + (1 if pat["days"] >= 40 else 0)
    elif k == "box":
        it.append(item("good", "型態", f"盤整 {pat['days']} 天後帶量突破（{pat['bot']:,.2f}～{pat['top']:,.2f}）")); pts += 3 + (1 if pat["days"] >= 40 else 0)
    elif k == "high20": it.append(item("info", "型態", f"帶量突破 20 日高點 {pat['top']:,.2f}")); pts += 1
    elif k == "trend": it.append(item("good", "型態", "日線剛轉強、量能開始放大，尚未明顯噴遠")); pts += 2
    elif k == "strong": it.append(item("good", "型態", f"強勢續攻，近3日約 {pat.get('ret3', 0):.1f}% 且量能放大")); pts += 3
    else: it.append(item("good", "型態", "多頭趨勢中拉回支撐，量縮止跌")); pts += 3
    if k != "pullback":
        vol_label = "近期最大" if k in ("trend", "strong") else "突破當天"
        it.append(item("good" if pat["vr"] >= 2 else "info", "量能", f"{vol_label} {pat['vr']:.1f} 倍量")); pts += 1 if pat["vr"] >= 2 else 0
    if ms["zero"]: it.append(item("good", "MACD", "零軸附近黃金交叉，起漲訊號")); pts += 2
    elif ms["golden"]: it.append(item("info", "MACD", "黃金交叉（離零軸較遠）"))
    elif ms["dead"] and k == "pullback": it.append(item("info", "MACD", "回檔中，之後重新黃金交叉會更確定"))
    elif ms["dead"]: it.append(item("bad", "MACD", "死亡交叉，動能不足")); pts -= 2
    if ms["rising"]: it.append(item("good", "MACD", "紅柱連續放大，力道增強")); pts += 1
    if k == "pullback": it.append(item("good", "RSI", f"RSI {rsi:.0f}，回檔守在健康區")); pts += 1
    elif 50 <= rsi <= 75: it.append(item("good", "RSI", f"RSI {rsi:.0f}，剛轉強還沒過熱")); pts += 1
    else: it.append(item("info", "RSI", f"RSI {rsi:.0f}"))
    if I["ma60"].iloc[-1] > I["ma60"].iloc[-6]: it.append(item("good", "季線", "季線往上，中期趨勢向上")); pts += 1
    return it, pts


def tech_items_hold(df, I, ms, support, resistance):
    c = df["Close"]; close = float(c.iloc[-1]); m20, m60 = I["ma20"].iloc[-1], I["ma60"].iloc[-1]; rsi = I["rsi"].iloc[-1]
    it = []
    if m60 == m60 and close < m60: it.append(item("bad", "趨勢", f"跌破季線（{m60:,.2f}），該檢討"))
    elif close < m20: it.append(item("warn", "趨勢", f"跌破月線（{m20:,.2f}），留意；季線 {m60:,.2f} 是底線"))
    else: it.append(item("good", "趨勢", f"站穩月線（{m20:,.2f}）和季線（{m60:,.2f}）"))
    if rsi > RSI_HOT: it.append(item("warn", "RSI", f"RSI {rsi:.0f} 過熱，可考慮先停利一部分"))
    elif I["rsi"].iloc[-10:].max() >= 70 and rsi < 50: it.append(item("warn", "RSI", f"RSI 從高檔跌到 {rsi:.0f}，上漲力道轉弱"))
    else: it.append(item("info", "RSI", f"RSI {rsi:.0f}"))
    if ms["div"]: it.append(item("warn", "MACD", "股價創高但 MACD 沒跟上（背離），漲勢可能後繼無力"))
    elif ms["dead"]: it.append(item("warn", "MACD", "死亡交叉，動能轉弱"))
    elif ms["golden"]: it.append(item("good", "MACD", "黃金交叉，動能轉強"))
    elif I["hist"].iloc[-1] > 0: it.append(item("info", "MACD", "紅柱，多方力道"))
    else: it.append(item("info", "MACD", "綠柱，力道偏弱"))
    if support:
        d = (close / support - 1) * 100; it.append(item("info", "支撐", f"下方支撐 {support:,.2f}（距離 {d:.1f}%）"))
    if resistance:
        d = (resistance / close - 1) * 100; it.append(item("warn" if d <= 3 else "info", "壓力", f"上方壓力 {resistance:,.2f}（還有 {d:.1f}%）"))
    else: it.append(item("good", "壓力", "上方沒有前高壓力"))
    return it


def chip_items(code, hist, chips_ok, bhist, mhist, pc5):
    it = []
    if chips_ok and any(d and code in d for d in hist):
        today = (hist[-1] or {}).get(code); tb = streak(chip_series(hist, code, "trust")); ts = streak(chip_series(hist, code, "trust"), positive=False); al = streak(chip_series(hist, code, "total"), positive=False)
        if al >= 3: it.append(item("bad", "法人", f"三大法人連賣 {al} 天"))
        elif ts >= 3: it.append(item("bad", "法人", f"投信連賣 {ts} 天"))
        elif tb >= 3: it.append(item("good", "法人", f"投信連買 {tb} 天"))
        elif today and today["foreign"] > 0 and today["trust"] > 0: it.append(item("good", "法人", "外資、投信同步買超"))
        elif today: it.append(item("info", "法人", f"外資 {today['foreign']:+,.0f} 張、投信 {today['trust']:+,.0f} 張"))
    else: it.append(item("info", "法人", "沒有法人資料（上櫃股暫不支援）"))

    b = bhist.get(code)
    if b:
        pct = b[-1][1]
        if len(b) >= 2:
            d = pct - b[-2][1]; s = "good" if d >= 0.5 else "bad" if d <= -0.5 else "info"
            it.append(item(s, "大戶", f"400 張以上大戶持股 {pct:.1f}%，比上週 {d:+.2f}%"))
        else: it.append(item("info", "大戶", f"400 張以上大戶持股 {pct:.1f}%（下週起看得到變化）"))

    m = mhist.get(code)
    if m:
        fin, sh = m[-1][1], m[-1][2]; old = m[max(0, len(m) - 6)]; done = False
        if len(m) >= 3 and old[1] > 0:
            fc = (fin / old[1] - 1) * 100
            if fc >= 10 and pc5 > 0: it.append(item("warn", "融資", f"融資近幾天增加 {fc:.0f}%，散戶在追")); done = True
            elif fc <= -5 and pc5 > 0: it.append(item("good", "融資", f"股價漲、融資減少 {abs(fc):.0f}%，籌碼乾淨")); done = True
            if old[2] > 0 and (sh / old[2] - 1) * 100 >= 20 and sh >= 500: it.append(item("good", "融券", f"融券增加到 {sh:,.0f} 張，有軋空機會")); done = True
        if not done: it.append(item("info", "融資券", f"融資 {fin:,.0f} 張、融券 {sh:,.0f} 張（前一交易日）"))
    return it


def ym_text(ym):
    try: return f"{int(ym[-2:])}月"
    except Exception: return "最新月"


def fund_items(f, close):
    if not f: return [], {}
    it = []; pe, ind_pe, pb = f.get("pe"), f.get("ind_pe"), f.get("pb")
    eps = close / pe if pe else None; roe = pb / pe * 100 if pe and pb else None; fair = eps * ind_pe if eps and ind_pe else None; ind = f.get("industry") or "同產業"
    if eps: it.append(item("info", "獲利", f"近四季每股賺 {eps:.2f} 元"))
    elif f.get("has_pe"): it.append(item("bad", "獲利", "近四季沒有獲利"))
    if roe is not None:
        if roe >= 15: it.append(item("good", "ROE", f"ROE 約 {roe:.1f}%，賺錢效率好"))
        elif roe >= 8: it.append(item("info", "ROE", f"ROE 約 {roe:.1f}%"))
        else: it.append(item("warn", "ROE", f"ROE 約 {roe:.1f}%，賺錢效率偏低"))
    if pe and ind_pe:
        r = pe / ind_pe
        if r < 0.8: it.append(item("good", "評價", f"本益比 {pe:.1f} 倍，比{ind}平均 {ind_pe:.1f} 倍便宜"))
        elif r > 1.3: it.append(item("warn", "評價", f"本益比 {pe:.1f} 倍，比{ind}平均 {ind_pe:.1f} 倍貴"))
        else: it.append(item("info", "評價", f"本益比 {pe:.1f} 倍，跟{ind}平均 {ind_pe:.1f} 倍差不多"))
    yoy, cum = f.get("rev_yoy"), f.get("rev_cum")
    if yoy is not None:
        m = ym_text(f.get("rev_ym", "")); tail = f"，今年累計 {cum:+.1f}%" if cum is not None else ""
        if yoy >= 20: it.append(item("good", "營收", f"{m}年增 {yoy:.1f}%，成長強勁{tail}"))
        elif yoy >= 0: it.append(item("info", "營收", f"{m}年增 {yoy:.1f}%{tail}"))
        elif yoy > -20: it.append(item("warn", "營收", f"{m}年減 {abs(yoy):.1f}%{tail}"))
        else: it.append(item("bad", "營收", f"{m}年減 {abs(yoy):.1f}%，衰退明顯{tail}"))
    if f.get("yield") and f["yield"] >= 5: it.append(item("good", "股息", f"殖利率 {f['yield']:.1f}%"))
    vals = {"eps": r2(eps), "pe": r2(pe), "ind_pe": r2(ind_pe), "fair": r2(fair), "roe": r2(roe),
            "yield": r2(f.get("yield")), "pb": r2(pb), "rev_yoy": r2(yoy)}
    return it, vals


def news_items(f, news):
    it = []
    for n in (f or {}).get("notices", [])[:3]:
        bad = any(w in n["title"] for w in NOTICE_NEG); it.append(item("bad" if bad else "info", "重大訊息", n["title"]))
    if news:
        p = sum(n["tag"] == "pos" for n in news); q = sum(n["tag"] == "neg" for n in news)
        it.append(item("info", "新聞", f"近 7 天 {len(news)} 則（標題偏多 {p}、偏空 {q}，僅供參考）"))
    return it


def facet(items): return {"v": verdict_of(items) if items else "neutral", "items": items}
def facet_pts(items):
    return sum(1 for i in items if i["s"] == "good") - sum(1 for i in items if i["s"] in ("bad",)) - 0.5 * sum(1 for i in items if i["s"] == "warn")


def make_plan(pat, close, support, resistance, df):
    if pat["kind"] == "pullback":
        lo, hi = support, support * 1.02; stop = support * 0.97
        if resistance: target, tnote = resistance, "前波壓力"
        else: target, tnote = float(df["High"].iloc[-20:].max()), "近期高點"
    elif pat["kind"] == "trend":
        base = support or float(df["Close"].rolling(20).mean().iloc[-1]); lo, hi = close * 0.99, close * 1.01; stop = base * 0.97
        if resistance and resistance > close * 1.03: target, tnote = resistance, "前波壓力"
        else: target, tnote = close * 1.10, "現價 +10%"
    elif pat["kind"] == "strong":
        base = support or float(df["Close"].rolling(20).mean().iloc[-1]); lo = max(base, close * 0.94); hi = min(close * 0.98, max(lo, base * 1.02))
        if hi < lo: hi = lo * 1.02
        stop = base * 0.97
        if resistance and resistance > close * 1.04: target, tnote = resistance, "前波壓力"
        else: target, tnote = close * 1.12, "強勢波段 +12%"
    else:
        top, bot = pat["top"], pat["bot"]; lo, hi = top, top * 1.02; stop = max(top * 0.97, bot); measured = top + (top - bot)
        if measured >= top * 1.10: target, tnote = measured, "箱型等幅"
        else: target, tnote = top * 1.10, "突破點 +10%"
        if resistance and hi * 1.05 < resistance < target: target, tnote = resistance, "前波壓力"
    mid = (lo + hi) / 2; rr = (target - mid) / (mid - stop) if mid > stop else None
    if close > hi: status = "現價高於進場區，等回測再進"
    elif close < lo: status = "現價低於進場區，等站回再進"
    else: status = "現價在進場區內"
    return {"lo": r2(lo), "hi": r2(hi), "stop": r2(stop), "target": r2(target), "tnote": tnote, "rr": r2(rr), "status": status}


def rotation(prices, last_day, funds, themes, today_chips):
    groups = {}
    for code, df in prices.items():
        if df.index[-1].date() != last_day or len(df) < 22: continue
        ind = (funds.get(code) or {}).get("industry")
        if ind: groups.setdefault(("產業", ind), []).append(code)
        for th in themes.get(code, []): groups.setdefault(("題材", th), []).append(code)
    def amt(df, i): return float(df["Close"].iloc[i] * df["Volume"].iloc[i])
    tot_t = sum(amt(prices[c], -1) for c in prices if prices[c].index[-1].date() == last_day)
    tot_a = sum(float((prices[c]["Close"] * prices[c]["Volume"]).iloc[-21:-1].mean()) for c in prices if prices[c].index[-1].date() == last_day and len(prices[c]) >= 22)
    if not tot_t or not tot_a: return {"inflow": [], "outflow": []}
    stats = []
    for (kind, name), codes in groups.items():
        if len(codes) < 3: continue
        at = sum(amt(prices[c], -1) for c in codes); aa = sum(float((prices[c]["Close"] * prices[c]["Volume"]).iloc[-21:-1].mean()) for c in codes)
        chg = sum((prices[c]["Close"].iloc[-1] / prices[c]["Close"].iloc[-2] - 1) * 100 for c in codes) / len(codes)
        inst = None
        if today_chips:
            inst = sum((today_chips.get(c) or {}).get("total", 0) * prices[c]["Close"].iloc[-1] * 1000 for c in codes) / 1e8
        stats.append({"name": name, "kind": kind, "n": len(codes), "chg": r2(chg), "ratio": r2(at / aa) if aa else None,
                      "share": r2((at / tot_t - aa / tot_a) * 100), "inst": r2(inst)})
    inflow = sorted([s for s in stats if s["chg"] > 0 and s["share"] > 0], key=lambda s: -s["share"])[:5]
    outflow = sorted([s for s in stats if s["chg"] < 0], key=lambda s: s["chg"])[:3]
    return {"inflow": inflow, "outflow": outflow}


FICON = {"good": "✅", "neutral": "➖", "bad": "⚠️"}
VICON = {"hold": "✅", "watch": "⚠️", "exit": "🚨"}
VWORD = {"hold": "續抱", "watch": "留意", "exit": "該檢討"}
KIND = {"tangle": "🔥", "box": "🚀", "high20": "🚀", "pullback": "🔄", "trend": "📈", "strong": "🚀"}


def facets_line(fc):
    return f"技術{FICON[fc['tech']['v']]} 籌碼{FICON[fc['chip']['v']]} 基本{FICON[fc['fund']['v']]} 消息{FICON[fc['news']['v']]}"


def rot_line(s):
    inst = f"｜法人 {s['inst']:+.1f}億" if s.get("inst") is not None else ""
    return f"{s['name']}（{s['kind']}）{s['chg']:+.1f}%｜成交值 {s['ratio']:.1f} 倍{inst}"


def pick_text(n, p):
    d = p["detail"]; pl = d["plan"]; th = "、".join(d.get("themes", [])[:2]) or d.get("industry") or ""
    L = [f"{n}. {KIND[p['kind']]}{p['code']} {d['name']}｜{th}"]
    sub = ""
    if p["kind"] not in ("pullback", "trend", "strong"): sub = f"　突破第 {p['day']} 天"
    L.append(f"收 {d['close']:,.2f}（{d['chg']:+.1f}%）{sub}")
    mtf = d.get("mtf") or {}
    if mtf: L.append(f"{mtf.get('label','')}｜多週期 {mtf.get('total',0)}/100（日{mtf.get('daily',0)}／週{mtf.get('weekly',0)}／月{mtf.get('monthly',0)}）")
    L.append(facets_line(d["facets"]))
    chipline = owner_chip_line(d)
    if chipline: L.append(chipline)
    support_txt = f"{d['support']:,.2f}" if d.get("support") is not None else "暫無明顯支撐"
    resistance_txt = f"{d['resistance']:,.2f}" if d.get("resistance") is not None else "無明顯前高壓力"
    L.append(f"支撐 {support_txt}｜壓力 {resistance_txt}")
    L.append(f"建議進場 {pl['lo']:,.2f}～{pl['hi']:,.2f}｜停損 {pl['stop']:,.2f}｜30天停利 {pl['target']:,.2f}（{pl['tnote']}）")
    L.append(f"→ {pl['status']}" + (f"，報酬風險比 {pl['rr']:.1f}" if pl.get("rr") else ""))
    fv = d["facets"]["fund"].get("vals") or {}; extra = [f"RSI {d['rsi']:.0f}"]
    if fv.get("pe"): extra.append(f"本益比 {fv['pe']:.1f}")
    if fv.get("roe") is not None: extra.append(f"ROE {fv['roe']:.0f}%")
    if fv.get("rev_yoy") is not None: extra.append(f"營收年增 {fv['rev_yoy']:+.0f}%")
    L.append("　".join(extra))
    warns = [i["d"] for k in ("chip", "fund", "news") for i in d["facets"][k]["items"] if i["s"] == "bad"]
    if warns: L.append("⚠️ " + "；".join(warns[:2]))
    return "\n".join(L)


def build_report(day, rot, lists, holdings=None, outflow_names=()):
    msgs = []
    L = [f"📊 {day:%m/%d} 盤後報告", "", "💸 資金流入"]
    L += [f"・{rot_line(s)}" for s in rot["inflow"]] or ["今天沒有明顯流入"]
    L += ["", "🧊 資金流出（持股在這些族群要留意）"]
    L += [f"・{rot_line(s)}" for s in rot["outflow"]] or ["今天沒有明顯流出"]
    L += ["", "四面：技術／籌碼／基本／消息  ✅好 ➖普通 ⚠️有疑慮", "輸入代號看完整健檢，輸入「說明」看用法", "※ 規則算出的參考，不是買賣建議"]
    msgs.append("\n".join(L))
    if holdings is not None:
        H = ["📌 我的持股"]
        if not holdings: H.append("還沒有持股，在 LINE 輸入「+代號」加入")
        for code, d in holdings:
            if not d:
                H.append(f"{code} 抓不到資料"); continue
            H.append(f"{VICON[d['verdict']]} {code} {d['name']} {d['close']:,.2f}（{d['chg']:+.1f}%）{VWORD[d['verdict']]}")
            chipline = owner_chip_line(d)
            if chipline: H.append("　" + chipline)
            alerts = [i["d"] for i in d["facets"]["tech"]["items"] if i["s"] in ("warn", "bad")]
            alerts += [i["d"] for k in ("chip", "fund", "news") for i in d["facets"][k]["items"] if i["s"] == "bad"]
            groups = [g for g in d.get("themes", []) + [d.get("industry")] if g in outflow_names]
            if groups: alerts.append(f"所屬 {groups[0]} 今天資金流出")
            if alerts: H.append("　" + "；".join(alerts[:3]))
        msgs.append("\n".join(H))
    titles = {"tangle": "🔥 起漲前段（均線糾結／箱型帶量剛突破）", "strong": "🚀 強勢續攻（已啟動但多頭結構仍完整）",
              "general": "📈 做多機會（突破／趨勢剛轉強）", "pullback": "🔄 多頭拉回再起（量縮守支撐）",
              "watch": "👀 偏多觀察（方向不差，但報酬風險比未達正式名單）"}
    for key in ("tangle", "strong", "general", "pullback", "watch"):
        items = lists[key]; T = [titles[key], ""]
        if not items: T.append("今天沒有符合的股票")
        for n, p in enumerate(items, 1): T.append(pick_text(n, p)); T.append("")
        msgs.append("\n".join(T).strip()[:4900])
    return msgs


def send_line(texts):
    token, uid = os.getenv("LINE_TOKEN"), os.getenv("LINE_USER_ID")
    if not token or not uid:
        print("\n\n=====\n\n".join(texts)); return
    for i in range(0, len(texts), 5):
        batch = texts[i:i + 5]
        r = requests.post("https://api.line.me/v2/bot/message/push", headers={"Authorization": f"Bearer {token}"},
                          json={"to": uid, "messages": [{"type": "text", "text": t} for t in batch]}, timeout=30)
        print(f"LINE 回應 第 {i // 5 + 1} 批：", r.status_code, r.text[:200])
        if r.status_code >= 300: break
        time.sleep(1)


def main():
    names = get_universe(); owner_codes, all_codes = get_holdings()
    print(f"上市＋上櫃股票 {len(names)} 檔，持股（所有人）{len(all_codes)} 檔")
    target_day = get_latest_twse_trade_day()
    if target_day: print(f"證交所最近交易日：{target_day}")
    else: print("⚠️ 無法確認證交所最近交易日，改用 Yahoo 最新資料")
    prices = get_prices(list(names), target_day)
    if "2330" not in prices: sys.exit("抓不到股價資料，結束")
    if target_day and prices["2330"].index[-1].date() < target_day: patch_latest_twse_day(prices, target_day)
    for code in all_codes:
        if code not in prices:
            df = get_single_price(code)
            if df is not None: prices[code] = df
    trade_days = [d.date() for d in prices["2330"].index]; last_day = trade_days[-1]; today_tw = dt.datetime.now(TW).date(); is_new_day = last_day == today_tw
    stale_market_data = target_day is not None and last_day < target_day
    print(f"最終 2330 最新日 K：{last_day}")
    if stale_market_data: print(f"⚠️ Yahoo Finance 行情落後：證交所最近交易日 {target_day}，Yahoo 只有 {last_day}；停止推播，避免送出舊報告。")
    hist = get_chip_history(trade_days[-CHIP_DAYS:]); today_chips = hist[-1]; chips_ok = today_chips is not None
    funds = get_fundamentals(); themes = load_themes()
    H = load_prev_hist(); H.setdefault("big", {}); H.setdefault("margin", {})
    bdate, big = get_big_holders()
    for code, pct in big.items():
        arr = H["big"].setdefault(code, [])
        if not arr or arr[-1][0] != bdate: arr.append([bdate, round(pct, 2)])
        H["big"][code] = arr[-8:]
    for code, (fin, sh) in get_margin().items():
        arr = H["margin"].setdefault(code, [])
        if not arr or arr[-1][0] != str(last_day): arr.append([str(last_day), fin, sh])
        H["margin"][code] = arr[-10:]

    details, cands = {}, []
    for code, df in prices.items():
        if len(df) < 70: continue
        try:
            I = indicators(df); c = df["Close"]; close = float(c.iloc[-1]); ma20 = float(I["ma20"].iloc[-1])
            support, resistance = find_levels(df, ma20); ms = macd_state(c, I); pc5 = (close / c.iloc[-6] - 1) * 100
            f = funds.get(code); fi, fvals = fund_items(f, close); ci = chip_items(code, hist, chips_ok, H["big"], H["margin"], pc5)
            ti = tech_items_hold(df, I, ms, support, resistance); mtf = timeframe_score(df, I, ms)
            chip_today = (today_chips or {}).get(code) or {}
            bseries = H["big"].get(code) or []
            big_pct = bseries[-1][1] if bseries else None
            d = {"name": names.get(code, ""), "date": str(df.index[-1].date()), "close": r2(close), "chg": r2((close / c.iloc[-2] - 1) * 100),
                 "industry": (f or {}).get("industry", ""), "themes": themes.get(code, []), "ma20": r2(ma20), "ma60": r2(I["ma60"].iloc[-1]),
                 "rsi": r2(I["rsi"].iloc[-1]), "support": r2(support), "resistance": r2(resistance), "mtf": mtf,
                 "big_pct": r2(big_pct), "chip_today": {k: r2(v) for k, v in chip_today.items()} if chip_today else {},
                 "closes": [r2(x) for x in c.iloc[-60:]], "ma20s": [r2(x) for x in I["ma20"].iloc[-60:]], "ma60s": [r2(x) for x in I["ma60"].iloc[-60:]],
                 "facets": {"tech": facet(ti), "chip": facet(ci), "fund": {**facet(fi), "vals": fvals}, "news": facet(news_items(f, []))}}
            bad = sum(i["s"] == "bad" for i in ti); warn = sum(i["s"] == "warn" for i in ti)
            if close < (I["ma60"].iloc[-1] or 0) or bad >= 2: d["verdict"], d["summary"] = "exit", "跌破季線或多項轉弱，該檢討是否出場"
            elif bad or warn >= 2 or close < ma20: d["verdict"], d["summary"] = "watch", "還沒壞，但有警訊，抱著要守好停損"
            else: d["verdict"], d["summary"] = "hold", "趨勢健康，可以續抱"
            d["stop_ref"] = r2(max(support or 0, 0) * 0.97) if support else r2(I["ma60"].iloc[-1]); details[code] = d
            if code not in names or df.index[-1].date() != last_day: continue
            if I["vma20"].iloc[-2] / 1000 < MIN_AVG_VOLUME_LOTS or not (MIN_PRICE <= close <= MAX_PRICE): continue
            rsi = float(I["rsi"].iloc[-1]); bias = (close / ma20 - 1) * 100; pat = detect_breakout(df, I)
            if pat:
                low120 = float(df["Low"].iloc[-120:].min()); rise = (pat["top"] / low120 - 1) * 100; limit = NOT_HIGH_TANGLE if pat["kind"] == "tangle" else NOT_HIGH_GENERAL
                if rsi > RSI_HOT or bias > MAX_BIAS or rise > limit: pat = None
            if not pat: pat = detect_strong_continuation(df, I, support)
            if not pat: pat = detect_pullback(df, I, support)
            if not pat: pat = detect_trend_setup(df, I, support)
            if not pat: continue
            if mtf["total"] < MTF_MIN_SCORE: continue
            tpi, tpts = tech_items_pick(pat, I, ms, rsi); cands.append((code, pat, tpi, tpts))
        except Exception as e: print(code, "分析失敗：", e)

    lists = {"tangle": [], "strong": [], "general": [], "pullback": [], "watch": []}
    for code, pat, tpi, tpts in cands:
        d = details[code]; tech_score = d["mtf"]["total"] * 0.70 + tpts * 2.0; chip_score = facet_pts(d["facets"]["chip"]["items"]) * 3.0; fund_score = facet_pts(d["facets"]["fund"]["items"]) * 1.0
        score = tech_score + chip_score + fund_score; plan = make_plan(pat, d["close"], d["support"], d["resistance"], prices[code]); rr = plan.get("rr")
        if rr is None or rr < RR_WATCH_MIN: continue
        if rr < RR_FORMAL_MIN: key = "watch"
        elif pat["kind"] == "strong": key = "strong"
        elif pat["kind"] == "tangle": key = "tangle"
        elif pat["kind"] == "pullback": key = "pullback"
        else: key = "general"
        lists[key].append({"code": code, "kind": pat["kind"], "day": pat.get("day"), "score": round(score, 1), "pat": pat, "tpi": tpi, "plan": plan})
    for key in lists:
        lists[key].sort(key=lambda p: (-p["score"], -(p["plan"].get("rr") or 0))); lists[key] = lists[key][:LIST_N]

    news_codes = list(dict.fromkeys(all_codes + [p["code"] for k in lists for p in lists[k]]))
    for code in news_codes:
        if code in details:
            nw = get_news(code, details[code]["name"]); details[code]["news"] = nw; details[code]["facets"]["news"] = facet(news_items(funds.get(code), nw)); time.sleep(1)
    for key in lists:
        for p in lists[key]:
            d = details[p["code"]]; df = prices[p["code"]]; d["plan"] = p.get("plan") or make_plan(p["pat"], d["close"], d["support"], d["resistance"], df)
            d["pick"] = {"list": key, "kind": p["kind"], "day": p["day"], "score": p["score"]}; d["facets"]["tech"] = facet(p["tpi"]); p["detail"] = d

    rot = rotation(prices, last_day, funds, themes, today_chips); outflow_names = {s["name"] for s in rot["outflow"]}
    os.makedirs("site/s", exist_ok=True)
    for code, d in details.items():
        with open(f"site/s/{code}.json", "w", encoding="utf-8") as fp: json.dump(d, fp, ensure_ascii=False, separators=(",", ":"))
    public_report = build_report(last_day, rot, lists)
    index = {"date": str(last_day), "updated": dt.datetime.now(TW).strftime("%Y-%m-%d %H:%M"), "names": {c: [d["name"], d["verdict"]] for c, d in details.items()},
             "rotation": rot, "report": public_report,
             "lists": {k: [{"code": p["code"], "name": p["detail"]["name"], "kind": p["kind"], "day": p["day"], "score": p["score"],
                            "themes": p["detail"]["themes"], "industry": p["detail"]["industry"], "close": p["detail"]["close"], "chg": p["detail"]["chg"],
                            "facets": {f: p["detail"]["facets"][f]["v"] for f in ("tech", "chip", "fund", "news")}, "plan": p["detail"]["plan"]} for p in v] for k, v in lists.items()}}
    with open("site/index.json", "w", encoding="utf-8") as fp: json.dump(index, fp, ensure_ascii=False, separators=(",", ":"))
    with open("site/hist.json", "w", encoding="utf-8") as fp: json.dump(H, fp, ensure_ascii=False, separators=(",", ":"))
    if os.path.exists("index.html"):
        with open("index.html", encoding="utf-8") as a, open("site/index.html", "w", encoding="utf-8") as b: b.write(a.read())
    print(f"網站資料：{len(details)} 檔；名單 起漲 {len(lists['tangle'])}、強勢 {len(lists['strong'])}、做多 {len(lists['general'])}、拉回 {len(lists['pullback'])}、觀察 {len(lists['watch'])}")
    if stale_market_data: return
    if not is_new_day and os.getenv("FORCE") != "true":
        print("今天沒有新資料（可能休市），只更新網站，不推 LINE"); return
    owner_hold = [(c, details.get(c)) for c in owner_codes]
    send_line(build_report(last_day, rot, lists, owner_hold, outflow_names))


if __name__ == "__main__":
    main()
