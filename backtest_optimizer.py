import os
import time
import datetime
import requests
from itertools import product

# Прокси Happ
PROXY_URL = "http://127.0.0.1:10809"
os.environ['HTTP_PROXY'] = PROXY_URL
os.environ['HTTPS_PROXY'] = PROXY_URL

# --- НАСТРОЙКИ ---
SYMBOL = 'BTCUSDT'
INTERVAL = '4h'
START_DATE = '2025-01-01'
END_DATE = '2026-08-08'

# Параметры для перебора
ENTRY_PERIODS = [15, 20, 25, 30, 40, 55]
EXIT_PERIODS = [5, 10, 15, 20]

USE_TREND_FILTER = True
TREND_MA = 200
TRADE_AMOUNT = 1000.0
USE_PYRAMIDING = True
PYRAMID_STEP = 1.0
MAX_UNITS = 3
START_USDT = 10000.0
FEE = 0.001

KLINES_URL = "https://api.binance.com/api/v3/klines"


def fetch_klines():
    print(f"⏳ Качаю историю: {START_DATE} → {END_DATE}, {INTERVAL}...")
    start_ts = int(datetime.datetime.strptime(START_DATE, '%Y-%m-%d').timestamp() * 1000)
    end_ts = int(datetime.datetime.strptime(END_DATE, '%Y-%m-%d').timestamp() * 1000)
    candles = []
    current = start_ts
    while current < end_ts:
        resp = requests.get(KLINES_URL, params={
            'symbol': SYMBOL, 'interval': INTERVAL,
            'startTime': current, 'limit': 1000,
        }, timeout=20)
        resp.raise_for_status()
        chunk = resp.json()
        if not chunk:
            break
        candles += chunk
        current = chunk[-1][0] + 1
        time.sleep(0.3)
    print(f"✅ Загружено свечей: {len(candles)}\n")
    return candles


def ma(vals, p):
    return sum(vals[-p:]) / p


def backtest(candles, entry_period, exit_period):
    """Запускает один бэктест с заданными параметрами"""
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]

    usdt = START_USDT
    units = []
    trades = []

    start_i = max(entry_period, TREND_MA if USE_TREND_FILTER else 0) + 1

    for i in range(start_i, len(closes)):
        price = closes[i]

        if not units:
            hh = max(highs[i - entry_period:i])
            if price > hh:
                if USE_TREND_FILTER and price < ma(closes[:i + 1], TREND_MA):
                    continue
                spend = min(TRADE_AMOUNT, usdt)
                if spend <= 0:
                    continue
                btc_qty = spend * (1 - FEE) / price
                usdt -= spend
                units.append((price, btc_qty))
        else:
            last_entry = units[-1][0]
            ll = min(lows[i - exit_period:i])
            if price < ll:
                total_btc = sum(u[1] for u in units)
                total_spent = sum(u[0] * u[1] / (1 - FEE) for u in units)
                got = total_btc * price * (1 - FEE)
                usdt += got
                pnl = got - total_spent
                trades.append(pnl)
                units = []
                continue

            if USE_PYRAMIDING and len(units) < MAX_UNITS:
                if price >= last_entry * (1 + PYRAMID_STEP / 100):
                    spend = min(TRADE_AMOUNT, usdt)
                    if spend > 0:
                        btc_qty = spend * (1 - FEE) / price
                        usdt -= spend
                        units.append((price, btc_qty))

    # Если позиция осталась - закрываем по последней цене
    if units:
        total_btc = sum(u[1] for u in units)
        total_spent = sum(u[0] * u[1] / (1 - FEE) for u in units)
        got = total_btc * closes[-1] * (1 - FEE)
        pnl = got - total_spent
        trades.append(pnl)

    if not trades:
        return None

    wins = [t for t in trades if t >= 0]
    losses = [t for t in trades if t < 0]
    gross_win = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0
    pf = gross_win / gross_loss if gross_loss > 0 else float('inf')
    total_pnl = sum(trades)

    return {
        'entry': entry_period,
        'exit': exit_period,
        'trades': len(trades),
        'winrate': len(wins) / len(trades) * 100 if trades else 0,
        'pf': pf,
        'pnl': total_pnl,
    }


def main():
    candles = fetch_klines()
    
    print(f"🔬 Перебираю {len(ENTRY_PERIODS) * len(EXIT_PERIODS)} комбинаций параметров...\n")
    results = []
    
    for entry, exit in product(ENTRY_PERIODS, EXIT_PERIODS):
        result = backtest(candles, entry, exit)
        if result:
            results.append(result)
            print(f"  ENTRY={entry:2d}, EXIT={exit:2d} | сделок {result['trades']:3d} | WR {result['winrate']:.0f}% | PF {result['pf']:.2f} | P&L {result['pnl']:+.2f}")
    
    # Сортируем по Profit Factor (только те, у кого PF > 1)
    profitable = [r for r in results if r['pf'] > 1.0]
    profitable.sort(key=lambda x: x['pf'], reverse=True)
    
    print("\n" + "=" * 80)
    print("🏆 ТОП-5 ПРИБЫЛЬНЫХ КОМБИНАЦИЙ (PF > 1.0):")
    print("=" * 80)
    for i, r in enumerate(profitable[:5], 1):
        print(f"{i}. ENTRY={r['entry']:2d}, EXIT={r['exit']:2d} | сделок {r['trades']:3d} | WR {r['winrate']:.0f}% | PF {r['pf']:.2f} | P&L {r['pnl']:+.2f} USDT")
    
    if not profitable:
        print("❌ Прибыльных комбинаций не найдено. Стратегия не работает на этом периоде.")
        print("Рекомендация: попробовать другой таймфрейм (4h) или другой рынок.")
    else:
        best = profitable[0]
        print("\n" + "=" * 80)
        print("✅ РЕКОМЕНДУЕМЫЕ ПАРАМЕТРЫ:")
        print("=" * 80)
        print(f"ENTRY_PERIOD = {best['entry']}")
        print(f"EXIT_PERIOD = {best['exit']}")
        print(f"\nВставь их в backtest_breakout.py и проверь ещё раз!")


if __name__ == "__main__":
    main()