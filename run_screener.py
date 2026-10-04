# -*- coding: utf-8 -*-
"""
正式執行入口：
- 不限制股價上下限
- 保留流動性門檻
- 優先找站上月線／季線、量價轉強、結構乾淨的做多股
- 5 日線上穿 10 日線（日線均線黃金交叉）是加分，不是唯一必要條件
- 修正上櫃股票 .TWO 代號被誤轉成 6548O／8155O 的問題
"""
import screener

# 股價不設上下限
screener.MIN_PRICE = float("-inf")
screener.MAX_PRICE = float("inf")


# ---- 修正上櫃股票代號 ----
# 原本 screener.get_prices() 先 replace('.TW')，會把 8155.TWO 變成 8155O。
# 在正式入口把結果統一校正回真正的四碼股票代號，讓選股、網站、LINE 查詢都用同一套代號。
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


def recent_ma_golden(I, lookback=5):
    """最近 lookback 個交易日內，5 日線是否上穿 10 日線。"""
    n = len(I)
    for k in range(1, lookback + 1):
        i = n - k
        if i - 1 < 0:
            continue
        if I["ma5"].iloc[i - 1] <= I["ma10"].iloc[i - 1] and I["ma5"].iloc[i] > I["ma10"].iloc[i]:
            return True
    return False


def clean_structure(df, I):
    """正式候選優先要求股價站上月線與季線，避免上方季線立即形成壓力。"""
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


if __name__ == "__main__":
    screener.main()
