import ccxt
import pandas as pd
import json

symbol = 'XAUT/USDT'
formatted_symbol = symbol.replace('/', '')
timeframe = '5m'
limit = 200  # ۲۰۰ کندل آخر: همه‌ی این ۲۰۰ تا برای مرحله ۱ (تحلیل SMC) استفاده
             # می‌شه؛ ۱۰۰ تای آخرش هم (زیرمجموعه‌ی همین ۲۰۰ تا) برای محاسبه‌ی
             # FVG مرحله ۲ به کار می‌ره. یک بار فچ کافیه چون ۱۰۰ زیرمجموعه‌ی ۲۰۰ هست.

exchange = ccxt.toobit()
candles = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

df = pd.DataFrame(candles, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
df['Time'] = pd.to_datetime(df['Timestamp'], unit='ms').dt.strftime('%Y.%m.%d %H:%M')

# ---------------------------------------------------------------------------
# 1) داده خام ۲۰۰ کندل آخر -> ورودی پرامپت ثابت مرحله ۱ (تحلیل SMC/RTM)
#    این اسکریپت خودِ پرامپت رو نمی‌سازه؛ پرامپت‌های مرحله ۱ و ۲ کاملاً ثابت
#    و مشترک بین هر ۷ پروژه هستن و داخل M.py نگه‌داری می‌شن. اینجا فقط داده
#    خام تولید می‌شه.
# ---------------------------------------------------------------------------
stage1_candles = []
for _, row in df.iterrows():
    stage1_candles.append({
        "Time": row['Time'],
        "Open": float(row['Open']),
        "High": float(row['High']),
        "Low": float(row['Low']),
        "Close": float(row['Close']),
        "Volume": float(row['Volume'])
    })

# ---------------------------------------------------------------------------
# 2) شناسایی Fair Value Gap ها فقط روی ۱۰۰ کندل آخر -> ورودی پرامپت ثابت مرحله ۲
# ---------------------------------------------------------------------------
df_fvg = df.tail(100).reset_index(drop=True)
stage2_fvg_list = []

for i in range(2, len(df_fvg)):
    c1 = df_fvg.iloc[i - 2]
    c2 = df_fvg.iloc[i - 1]
    c3 = df_fvg.iloc[i]

    if c3['Low'] > c1['High']:
        fvg_top = float(c3['Low'])
        fvg_bottom = float(c1['High'])
        stage2_fvg_list.append({
            "symbol": formatted_symbol,
            "type": "Bullish",
            "middle_candle_time": c2['Time'],
            "candle_1_time": c1['Time'],
            "candle_3_time": c3['Time'],
            "fvg_top": round(fvg_top, 2),
            "fvg_bottom": round(fvg_bottom, 2),
            "fvg_ce": round((fvg_top + fvg_bottom) / 2, 2),
            "fvg_size": round(fvg_top - fvg_bottom, 2),
            "displacement_volume": float(c2['Volume'])
        })

    elif c3['High'] < c1['Low']:
        fvg_top = float(c1['Low'])
        fvg_bottom = float(c3['High'])
        stage2_fvg_list.append({
            "symbol": formatted_symbol,
            "type": "Bearish",
            "middle_candle_time": c2['Time'],
            "candle_1_time": c1['Time'],
            "candle_3_time": c3['Time'],
            "fvg_top": round(fvg_top, 2),
            "fvg_bottom": round(fvg_bottom, 2),
            "fvg_ce": round((fvg_top + fvg_bottom) / 2, 2),
            "fvg_size": round(fvg_top - fvg_bottom, 2),
            "displacement_volume": float(c2['Volume'])
        })

# ---------------------------------------------------------------------------
# 3) یک فایل JSON واحد شامل دیتای هر دو مرحله (بدون هیچ متن پرامپتی داخلش)
# ---------------------------------------------------------------------------
output_data = {
    "symbol": formatted_symbol,
    "timeframe": timeframe,
    "stage1_candles_count": len(stage1_candles),
    "stage1_candles": stage1_candles,
    "stage2_fvg_window_candles": len(df_fvg),
    "stage2_fvg_count": len(stage2_fvg_list),
    "stage2_fvg_list": stage2_fvg_list
}

with open("XAUT-MarketData.json", "w", encoding="utf-8") as f:
    json.dump(output_data, f, indent=2, ensure_ascii=False)

print("File saved successfully: XAUT-MarketData.json")
