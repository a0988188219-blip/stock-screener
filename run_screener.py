# -*- coding: utf-8 -*-
"""
正式執行入口：
- 不限制股價上下限
- 保留流動性門檻
- 優先找站上月線／季線、量價轉強、結構乾淨的做多股
- 5 日線上穿 10 日線（日線均線黃金交叉）是加分，不是唯一必要條件
- 修正上櫃股票 .TWO 代號被誤轉成 6548O／8155O 的問題
- 上櫃股加入櫃買中心三大法人籌碼資料
- MACD 只認最近一次交叉，避免黃金交叉後仍殘留死亡交叉
"""
import io
import screener

# 股價不設上下限
screener.MIN_PRICE = float("-inf")
screener.MAX_PRICE = float("inf")


# ---- 修正上櫃股票代號 ----
_orig_get_prices = screener.get_prices


def get_prices_v2(codes, target_day=None):
    raw = _orig_get_prices(codes, target_day)
    fixed = {}
    for code, df in raw.items():
        real_code = code
        if len(code) == 5 and code.endswith("O"):
            candidate = code[:-1]
            if screener.MARKET_SUFFIX.get(candidate) == ".TWO":
                real_code = candidate
        fixed[real_code] = df
    return fixed


screener.get_prices = get_prices_v2


# ---- MACD：只認最近一次交叉 ----
def macd_state_v2(c, I):
    dif = I["dif"].values
    dea = I["dea"].values
    h = I["hist"].values
    n = len(dif)
    s = {"golden": False, "zero": False, "dead": False, "rising": False, "div": False}

    # 從最新往回找，第一個交叉就是目前真正有效的最近一次交叉。
    for i in range(n - 1, max(0, n - 10), -1):
        if i - 1 < 0:
            break
        if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            s["golden"] = True
            s["zero"] = abs(dif[i]) / max(abs(float(c.iloc[i])), 1e-9) <= 0.015
            break
        if dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
            s["dead"] = True
            break

    s["rising"] = bool(n >= 3 and h[-1] > h[-2] > h[-3] and h[-1] > 0)

    if n >= 60:
        cl = c.values
        if (cl[-10:].max() >= cl[-60:].max()
                and cl[-10:].max() > cl[-60:-10].max()
                and dif[-10:].max() < dif[-60:-10].max()):
            s["div"] = True
    return s


screener.macd_state = macd_state_v2


def recent_ma_golden(I, lookback=5):
    n = len(I)
    for k in range(1, lookback + 1):
        i = n - k
        if i - 1 < 0:
            continue
        if I["ma5"].iloc[i - 1] <= I["ma10"].iloc[i - 1] and I["ma5"].iloc[i] > I["ma10"].iloc[i]:
            return True
    return False


def clean_structure(df, I):
    close = float(df["Close"].iloc[-1])
    ma20 = float(I["ma20"].iloc[-1])
    ma60 = float(I["ma60"].iloc[-1])
    return close > ma20 and close > ma60


# ---- 型態先通過原本規則，再加一層『結構乾淨』過濾 ----
_orig_breakout = screener.detect_breakout
_orig_strong = screener.detect_strong_continuation
_orig_trend = screener.detect_trend_setup


def detect_breakout_v2(df, I):
    pat = _orig_breakout(df, I)
    if pat and not clean_structure(df, I):
        return None
    return pat


def detect_strong_v2(df, I, support):
    pat = _orig_strong(df, I, support)
    if pat and not clean_structure(df, I):
        return None
    return pat


def detect_trend_v2(df, I, support):
    pat = _orig_trend(df, I, support)
    if pat and not clean_structure(df, I):
        return None
    return pat


screener.detect_breakout = detect_breakout_v2
screener.detect_strong_continuation = detect_strong_v2
screener.detect_trend_setup = detect_trend_v2


# ---- 日線黃金交叉與均線多頭排列列為加分項 ----
_orig_tech_items_pick = screener.tech_items_pick


def tech_items_pick_v2(pat, I, ms, rsi):
    items, pts = _orig_tech_items_pick(pat, I, ms, rsi)
    if recent_ma_golden(I, 5):
        items.append(screener.item("good", "均線", "5日線近期上穿10日線，日線黃金交叉"))
        pts += 2
    if I["ma5"].iloc[-1] > I["ma10"].iloc[-1] > I["ma20"].iloc[-1]:
        items.append(screener.item("good", "均線", "5＞10＞20日線，多頭排列"))
        pts += 1
    if I["ma20"].iloc[-1] > I["ma20"].iloc[-6]:
        items.append(screener.item("good", "月線", "20日線上彎，短中期結構轉強"))
        pts += 1
    return items, pts


screener.tech_items_pick = tech_items_pick_v2


# ---- 上櫃股三大法人：接櫃買中心 TPEx ----
_orig_get_chips_one = screener.get_chips_one
_orig_chip_items = screener.chip_items


def _find_col(fields, must=(), exclude=()):
    for i, x in enumerate(fields):
        s = str(x)
        if all(k in s for k in must) and not any(k in s for k in exclude):
            return i
    return None


def _parse_tpex_table(fields, data):
    if not fields or not data:
        return {}
    i_code = _find_col(fields, ("代號",))
    i_foreign = _find_col(fields, ("外資", "買賣超"), ("自營商",))
    if i_foreign is None:
        i_foreign = _find_col(fields, ("外資及陸資", "淨買"), ("自營商",))
    i_trust = _find_col(fields, ("投信", "買賣超"))
    if i_trust is None:
        i_trust = _find_col(fields, ("投信", "淨買"))
    i_total = _find_col(fields, ("三大法人", "合計"))

    if i_code is None or i_foreign is None or i_trust is None:
        return {}

    out = {}
    for row in data:
        try:
            code = str(row[i_code]).strip()
            if len(code) != 4 or not code.isdigit() or code.startswith("0"):
                continue
            foreign = screener.to_num(row[i_foreign]) or 0
            trust = screener.to_num(row[i_trust]) or 0
            total = screener.to_num(row[i_total]) if i_total is not None else None
            dealer = 0 if total is None else total - foreign - trust
            if total is None:
                total = foreign + trust
            out[code] = {
                "foreign": foreign / 1000,
                "trust": trust / 1000,
                "dealer": dealer / 1000,
                "total": total / 1000,
            }
        except Exception:
            continue
    return out


def _flat_cols(df):
    cols = []
    for col in df.columns:
        parts = col if isinstance(col, tuple) else (col,)
        clean = []
        for x in parts:
            s = str(x).strip()
            if not s or s.lower() == "nan" or "Unnamed" in s:
                continue
            if not clean or clean[-1] != s:
                clean.append(s)
        cols.append(" ".join(clean))
    return cols


def _parse_tpex_html(text):
    try:
        tables = screener.pd.read_html(io.StringIO(text))
    except Exception:
        return {}

    for df in tables:
        if df.empty:
            continue
        fields = _flat_cols(df)
        joined = "|".join(fields)
        if "代號" not in joined or "投信" not in joined or "三大法人" not in joined:
            continue

        i_code = _find_col(fields, ("代號",))
        foreign_candidates = [
            i for i, s in enumerate(fields)
            if "外資及陸資" in s and "買賣超" in s and "自營商" not in s
        ]
        # 優先用「不含外資自營商」欄，和上市 T86 的外資定義較一致。
        i_foreign = next((i for i in foreign_candidates if "不含" in fields[i]), None)
        if i_foreign is None and foreign_candidates:
            i_foreign = foreign_candidates[0]

        i_trust = _find_col(fields, ("投信", "買賣超"))
        i_total = _find_col(fields, ("三大法人", "買賣超"))
        if i_total is None:
            i_total = _find_col(fields, ("三大法人", "合計"))
        if None in (i_code, i_foreign, i_trust, i_total):
            continue

        out = {}
        for row in df.itertuples(index=False, name=None):
            try:
                code = str(row[i_code]).strip()
                if len(code) != 4 or not code.isdigit() or code.startswith("0"):
                    continue
                foreign = screener.to_num(row[i_foreign]) or 0
                trust = screener.to_num(row[i_trust]) or 0
                total = screener.to_num(row[i_total]) or 0
                dealer = total - foreign - trust
                out[code] = {
                    "foreign": foreign / 1000,
                    "trust": trust / 1000,
                    "dealer": dealer / 1000,
                    "total": total / 1000,
                }
            except Exception:
                continue
        if out:
            return out
    return {}


def get_tpex_chips_one(date_str):
    try:
        y, m, d = int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8])
    except Exception:
        return {}

    roc = f"{y - 1911}/{m:02d}/{d:02d}"
    base = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"

    # 先試 JSON；櫃買中心若回 HTML/非 JSON，再改用官方 HTML 表格解析。
    try:
        r = screener.requests.get(base, params={"l": "zh-tw", "o": "json", "se": "EW", "t": "D", "d": roc},
                                  headers=screener.HEADERS, timeout=30)
        j = r.json()
        tables = j.get("tables") or []
        if isinstance(j.get("fields"), list) and isinstance(j.get("data"), list):
            tables = [{"fields": j.get("fields"), "data": j.get("data")}] + list(tables)
        for table in tables:
            parsed = _parse_tpex_table(table.get("fields") or [], table.get("data") or [])
            if parsed:
                print(f"櫃買中心 {date_str} 法人資料(JSON)：{len(parsed)} 檔")
                return parsed
    except Exception:
        pass

    try:
        r = screener.requests.get(base, params={"l": "zh-tw", "o": "htm", "se": "EW", "t": "D", "d": roc},
                                  headers=screener.HEADERS, timeout=30)
        parsed = _parse_tpex_html(r.text)
        if parsed:
            print(f"櫃買中心 {date_str} 法人資料(HTML)：{len(parsed)} 檔")
            return parsed
        print(f"{date_str} 上櫃法人資料：官方頁有回應，但未解析出明細")
    except Exception as e:
        print(f"{date_str} 上櫃法人資料抓取失敗：", e)
    return {}


def get_chips_one_all(date_str):
    listed = _orig_get_chips_one(date_str) or {}
    otc = get_tpex_chips_one(date_str) or {}
    merged = dict(listed)
    merged.update(otc)
    if otc:
        print(f"{date_str} 法人資料合併：上市 {len(listed)}、上櫃 {len(otc)}")
    return merged if merged else None


screener.get_chips_one = get_chips_one_all


def chip_items_v2(code, hist, chips_ok, bhist, mhist, pc5):
    items = _orig_chip_items(code, hist, chips_ok, bhist, mhist, pc5)
    for x in items:
        if x.get("d") == "沒有法人資料（上櫃股暫不支援）":
            x["d"] = "當日法人資料暫無"
    return items


screener.chip_items = chip_items_v2


if __name__ == "__main__":
    screener.main()
