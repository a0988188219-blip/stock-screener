from pathlib import Path

p = Path('screener.py')
s = p.read_text(encoding='utf-8')


def rep(old, new):
    global s
    if old not in s:
        raise SystemExit(f'patch target not found: {old[:80]!r}')
    s = s.replace(old, new, 1)

# 1) 不限制股價，只保留流動性門檻
rep('MIN_PRICE, MAX_PRICE = 10, 2000\n', '# 股價不設上下限；只用流動性、結構、量價與風險報酬篩選\n')
rep('if I["vma20"].iloc[-2] / 1000 < MIN_AVG_VOLUME_LOTS or not (MIN_PRICE <= close <= MAX_PRICE): continue',
    'if I["vma20"].iloc[-2] / 1000 < MIN_AVG_VOLUME_LOTS: continue')

# 2) 新增日線均線結構／黃金交叉訊號。黃金交叉是加分，不是唯一必要條件。
rep('    return s\n\n\ndef pivots(df, n=5, lookback=120):', '''    return s\n\n\ndef ma_signal(close, I):\n    """日線均線結構：5/10 黃金交叉加分，季線只作中期結構確認。"""\n    recent_cross = False\n    n = len(I)\n    for k in range(1, 6):\n        i = n - k\n        if i - 1 >= 0 and I["ma5"].iloc[i - 1] <= I["ma10"].iloc[i - 1] and I["ma5"].iloc[i] > I["ma10"].iloc[i]:\n            recent_cross = True\n            break\n    aligned = bool(I["ma5"].iloc[-1] > I["ma10"].iloc[-1] > I["ma20"].iloc[-1])\n    ma20_up = bool(I["ma20"].iloc[-1] > I["ma20"].iloc[-6])\n    ma60 = float(I["ma60"].iloc[-1])\n    above_ma60 = bool(close > ma60)\n    # 股價若仍在季線下方，尤其距離很近，視為上方壓力，不進正式推薦名單。\n    ma60_pressure = bool(close < ma60 and (ma60 / close - 1) <= 0.05)\n    bonus = 0\n    if recent_cross:\n        bonus += 4\n    if aligned:\n        bonus += 2\n    if ma20_up:\n        bonus += 1\n    if above_ma60:\n        bonus += 2\n    if ma60_pressure:\n        bonus -= 4\n    return {\n        "recent_cross_5_10": recent_cross,\n        "aligned_5_10_20": aligned,\n        "ma20_up": ma20_up,\n        "above_ma60": above_ma60,\n        "ma60_pressure": ma60_pressure,\n        "bonus": bonus,\n    }\n\n\ndef pivots(df, n=5, lookback=120):''')

# 3) 技術面文字與評分加入 5/10 黃金交叉、均線排列、季線位置。
rep('def tech_items_pick(pat, I, ms, rsi):\n', 'def tech_items_pick(pat, I, ms, rsi, mas):\n')
rep('    if ms["rising"]: it.append(item("good", "MACD", "紅柱連續放大，力道增強")); pts += 1\n', '''    if ms["rising"]: it.append(item("good", "MACD", "紅柱連續放大，力道增強")); pts += 1\n    if mas.get("recent_cross_5_10"):\n        it.append(item("good", "均線", "5日線近期上穿10日線，日線黃金交叉")); pts += 2\n    if mas.get("aligned_5_10_20"):\n        it.append(item("good", "均線", "5＞10＞20日線，多頭排列")); pts += 1\n    if mas.get("ma60_pressure"):\n        it.append(item("warn", "季線", "股價仍在季線下方且壓力很近，先列觀察")); pts -= 2\n    elif mas.get("above_ma60"):\n        it.append(item("good", "季線", "股價站上季線，中期上方壓力較小")); pts += 1\n''')

# 4) 每檔建立均線訊號，並放進輸出資料。
rep('support, resistance = find_levels(df, ma20); ms = macd_state(c, I); pc5 = (close / c.iloc[-6] - 1) * 100',
    'support, resistance = find_levels(df, ma20); ms = macd_state(c, I); mas = ma_signal(close, I); pc5 = (close / c.iloc[-6] - 1) * 100')
rep('"rsi": r2(I["rsi"].iloc[-1]), "support": r2(support), "resistance": r2(resistance), "mtf": mtf,',
    '"rsi": r2(I["rsi"].iloc[-1]), "support": r2(support), "resistance": r2(resistance), "mtf": mtf, "ma_signal": mas,')
rep('tpi, tpts = tech_items_pick(pat, I, ms, rsi); cands.append((code, pat, tpi, tpts))',
    'tpi, tpts = tech_items_pick(pat, I, ms, rsi, mas); cands.append((code, pat, tpi, tpts))')

# 5) 排名：結構與位置主導，黃金交叉只加分；季線下方只能進觀察名單。
rep('score = tech_score + chip_score + fund_score; plan = make_plan(pat, d["close"], d["support"], d["resistance"], prices[code]); rr = plan.get("rr")',
    'score = tech_score + chip_score + fund_score + (d.get("ma_signal") or {}).get("bonus", 0); plan = make_plan(pat, d["close"], d["support"], d["resistance"], prices[code]); rr = plan.get("rr")')
rep('if rr < RR_FORMAL_MIN: key = "watch"\n        elif pat["kind"] == "strong": key = "strong"',
    'if not (d.get("ma_signal") or {}).get("above_ma60", False): key = "watch"\n        elif rr < RR_FORMAL_MIN: key = "watch"\n        elif pat["kind"] == "strong": key = "strong"')

p.write_text(s, encoding='utf-8')
print('strategy patch applied')
