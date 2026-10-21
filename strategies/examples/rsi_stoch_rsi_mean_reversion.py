#!/usr/bin/env python3
"""
RSI + Stochastic RSI Mean Reversion Strategy for OpenAlgo
========================================================
Implements the exact rules from RSI_Stochastic_RSI_Mean_Reversion_Strategy.md

Strategy Summary:
- Long-only mean reversion trading strategy.
- Evaluated ONLY on completed candle close.
- Three mutually exclusive modes:
    1. Neutral: Entry on RSI(14) < 45 & StochRSI < 1 | TP = Entry + 5 | SL = Entry - 3
    2. Risk:    Entry on RSI(14) < 30 & StochRSI < 1 | TP = Entry + 10 | SL = Entry - 5
    3. Risky:   Entry on RSI(14) < 45 & StochRSI < 1 | Dynamic TP = EMA(9) - 1 | SL = Entry - 5
- Single active trade enforcement (one open trade at a time).
"""

import os
import sys
import time
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
from openalgo import api

# --- Configuration & Environment Variables ---
api_key   = os.getenv('OPENALGO_API_KEY', 'your_openalgo_api_key')
host      = os.getenv('HOST_SERVER', 'http://127.0.0.1:5000')
ws_url    = os.getenv('WEBSOCKET_URL', 'ws://127.0.0.1:8765')

symbol    = os.getenv('SYMBOL', 'RELIANCE')
exchange  = os.getenv('EXCHANGE', 'NSE')        # NSE, NFO, BSE, BFO, MCX
product   = os.getenv('PRODUCT', 'MIS')         # MIS (Intraday), NRML (Delivery/Options), CNC
quantity  = int(os.getenv('QUANTITY', '1'))
timeframe = os.getenv('TIMEFRAME', '15m')       # 1m, 5m, 15m, 30m, 1h
mode      = os.getenv('MODE', 'Neutral').capitalize() # Neutral, Risk, Risky

strategy_name = f"RSI_StochRSI_{mode}"

print("=" * 65)
print(f"🚀 Initializing OpenAlgo RSI + Stoch RSI Mean Reversion Strategy")
print(f"   Symbol:     {symbol} ({exchange})")
print(f"   Mode:       {mode}")
print(f"   Timeframe:  {timeframe}")
print(f"   Quantity:   {quantity}")
print(f"   Product:    {product}")
print(f"   Host:       {host}")
print("=" * 65)


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Calculate Wilder's RMA Relative Strength Index (RSI)."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(100.0)


def calculate_stoch_rsi(series: pd.Series, rsi_period: int = 14, stoch_period: int = 14, k_period: int = 3, d_period: int = 3):
    """Calculate Stochastic RSI (%K and %D) on 0-100 scale."""
    rsi = calculate_rsi(series, period=rsi_period)
    
    lowest_rsi = rsi.rolling(window=stoch_period).min()
    highest_rsi = rsi.rolling(window=stoch_period).max()
    
    range_rsi = highest_rsi - lowest_rsi
    stoch_raw = np.where(range_rsi > 0, ((rsi - lowest_rsi) / range_rsi) * 100.0, 0.0)
    stoch_series = pd.Series(stoch_raw, index=series.index)
    
    k = stoch_series.rolling(window=k_period).mean()
    d = k.rolling(window=d_period).mean()
    return rsi, k, d


def calculate_ema(series: pd.Series, period: int = 9) -> pd.Series:
    """Calculate Exponential Moving Average (EMA)."""
    return series.ewm(span=period, adjust=False).mean()


def run_strategy():
    client = api(api_key=api_key, host=host, ws_url=ws_url)

    # Active Position Tracking
    in_position = False
    entry_price = 0.0
    target_price = 0.0
    stop_loss_price = 0.0

    # Poll interval based on timeframe (e.g. check every 15s)
    poll_sleep = 15

    while True:
        try:
            now = datetime.now()
            start_date = (now - timedelta(days=5)).strftime("%Y-%m-%d")
            end_date = now.strftime("%Y-%m-%d")

            # Fetch historical candle bars via OpenAlgo
            df = client.history(
                symbol=symbol,
                exchange=exchange,
                interval=timeframe,
                start_date=start_date,
                end_date=end_date
            )

            if df is None or df.empty or len(df) < 20:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Waiting for sufficient candle data...")
                time.sleep(poll_sleep)
                continue

            # Ensure numeric close column
            df['close'] = df['close'].astype(float)
            closes = df['close']

            # Calculate indicators across all bars
            rsi_series, stoch_k_series, stoch_d_series = calculate_stoch_rsi(closes, 14, 14, 3, 3)
            ema_9_series = calculate_ema(closes, 9)

            # Signal evaluation on completed candle close (iloc[-2] is the last completed closed candle)
            closed_close   = float(closes.iloc[-2])
            closed_rsi     = float(rsi_series.iloc[-2])
            closed_stoch_k = float(stoch_k_series.iloc[-2])
            closed_ema_9   = float(ema_9_series.iloc[-2])

            # Current forming candle price for live tracking
            current_price  = float(closes.iloc[-1])

            timestamp_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"[{timestamp_str}] {symbol} LTP: {current_price:.2f} | Closed Bar: {closed_close:.2f} | RSI: {closed_rsi:.2f} | Stoch %K: {closed_stoch_k:.2f} | EMA 9: {closed_ema_9:.2f} | In Trade: {in_position}")

            # -------------------------------------------------------------
            # CASE 1: IN AN OPEN POSITION -> MONITOR EXITS (TP / SL)
            # -------------------------------------------------------------
            if in_position:
                effective_target = target_price
                if mode == "Risky":
                    # Dynamic Take Profit = EMA 9 - 1 point
                    dynamic_target = round(closed_ema_9 - 1.0, 2)
                    if dynamic_target > entry_price:
                        effective_target = dynamic_target

                pnl_pts = round(current_price - entry_price, 2)
                pnl_total = round(pnl_pts * quantity, 2)
                print(f"   📊 Position: LONG @ ₹{entry_price:.2f} | Current: ₹{current_price:.2f} | Target: ₹{effective_target:.2f} | SL: ₹{stop_loss_price:.2f} | P&L: ₹{pnl_total:+.2f} ({pnl_pts:+.2f} pts)")

                # Check Take Profit
                if current_price >= effective_target:
                    print(f"\n🎯 TAKE PROFIT TRIGGERED @ ₹{current_price:.2f} (Target: ₹{effective_target:.2f})")
                    resp = client.placesmartorder(
                        strategy=strategy_name,
                        symbol=symbol,
                        action="SELL",
                        exchange=exchange,
                        price_type="MARKET",
                        product=product,
                        quantity=quantity,
                        position_size=0
                    )
                    print(f"   Exit Order Response: {resp}")
                    in_position = False

                # Check Stop Loss
                elif current_price <= stop_loss_price:
                    print(f"\n🛑 STOP LOSS TRIGGERED @ ₹{current_price:.2f} (SL: ₹{stop_loss_price:.2f})")
                    resp = client.placesmartorder(
                        strategy=strategy_name,
                        symbol=symbol,
                        action="SELL",
                        exchange=exchange,
                        price_type="MARKET",
                        product=product,
                        quantity=quantity,
                        position_size=0
                    )
                    print(f"   Exit Order Response: {resp}")
                    in_position = False

            # -------------------------------------------------------------
            # CASE 2: NO POSITION -> EVALUATE ENTRY CONDITIONS
            # -------------------------------------------------------------
            else:
                entry_triggered = False

                if mode == "Neutral":
                    # Neutral Mode: RSI < 45 AND StochRSI < 1
                    if closed_rsi < 45.0 and closed_stoch_k < 1.0:
                        entry_triggered = True
                        target_price = round(current_price + 5.0, 2)
                        stop_loss_price = round(current_price - 3.0, 2)

                elif mode == "Risk":
                    # Risk Mode: RSI < 30 AND StochRSI < 1
                    if closed_rsi < 30.0 and closed_stoch_k < 1.0:
                        entry_triggered = True
                        target_price = round(current_price + 10.0, 2)
                        stop_loss_price = round(current_price - 5.0, 2)

                elif mode == "Risky":
                    # Risky Mode: RSI < 45 AND StochRSI < 1 | Dynamic Target = EMA 9 - 1
                    if closed_rsi < 45.0 and closed_stoch_k < 1.0:
                        entry_triggered = True
                        dynamic_target = round(closed_ema_9 - 1.0, 2)
                        target_price = dynamic_target if dynamic_target > current_price else round(current_price + 5.0, 2)
                        stop_loss_price = round(current_price - 5.0, 2)

                if entry_triggered:
                    print(f"\n⚡ BUY SIGNAL GENERATED [{mode} Mode] @ Closed Candle Close ₹{closed_close:.2f} (RSI: {closed_rsi:.2f}, Stoch: {closed_stoch_k:.2f})")
                    resp = client.placesmartorder(
                        strategy=strategy_name,
                        symbol=symbol,
                        action="BUY",
                        exchange=exchange,
                        price_type="MARKET",
                        product=product,
                        quantity=quantity,
                        position_size=quantity
                    )
                    print(f"   Entry Order Response: {resp}")
                    
                    in_position = True
                    entry_price = current_price
                    print(f"   ✅ Trade Entered @ ₹{entry_price:.2f} | Initial Target: ₹{target_price:.2f} | Stop Loss: ₹{stop_loss_price:.2f}")

        except KeyboardInterrupt:
            print("\n⏹️ Strategy stopped by user.")
            break
        except Exception as e:
            print(f"⚠️ Strategy runtime notice/error: {e}")
            time.sleep(poll_sleep)
            continue

        time.sleep(poll_sleep)


if __name__ == '__main__':
    run_strategy()
