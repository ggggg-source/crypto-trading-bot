import os
import time
import datetime
import requests

# Прокси Happ
PROXY_URL = "http://127.0.0.1:10809"
os.environ['HTTP_PROXY'] = PROXY_URL
os.environ['HTTPS_PROXY'] = PROXY_URL

# --- НАСТРОЙКИ (экспериментируй!) ---
SYMBOL = 'BTCUSDT'
INTERVAL = '5m'      # таймфрейм свечей
DAYS = 30            # сколько дней истории взять
FAST_MA = 10
SLOW_MA = 30
TREND_MA = 200       # долгосрочный тренд
RSI_PERIOD = 14      # RSI
RSI_MAX = 65         # не покупаем выше этого
TRADE_AMOUNT = 100.0
START_USDT = 10000.0
FEE = 0.001
COOLDOWN = 10        # пауза после сделки, в свечах

KLINES_URL = "https://api.binance.com/api/v3/klines"


def fetch_klines():
    print(f"⏳ Качаю историю: {DAYS} дн., {INTERVAL}...")
    candles = []
    end_time = int(time.time() * 1000)
    current = end_time - DAYS * 86400 * 1000
    while current < end_time:
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
        print(f"   загружено свечей: {len(candles)}")
        time.sleep(0.3)
    return candles


def ma(vals, p):
    if len(vals) < p:
        return None
    return sum(vals[-p:]) / p


def rsi(closes, period=14):
    """Простой RSI"""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def main():
    candles = fetch_klines()
    closes = [float(c[4]) for c in candles]
    times = [int(c[0]) for c in candles]
    print(f"\n✅ Загружено свечей: {len(closes)}\n")

    usdt = START_USDT
    btc = 0.0
    last_spend = 0.0
    trades = []
    last_trade_i = -10**9
    skipped_signals = 0  # считаем отфильтрованные сигналы

    for i in range(max(SLOW_MA, TREND_MA), len(closes)):
        if i - last_trade_i < COOLDOWN:
            continue
        fast = ma(closes[:i + 1], FAST_MA)
        slow = ma(closes[:i + 1], SLOW_MA)
        trend = ma(closes[:i + 1], TREND_MA)
        r = rsi(closes[:i + 1], RSI_PERIOD)
        price = closes[i]
        if fast is None or slow is None or trend is None or r is None:
            continue

        # СИГНАЛ НА ПОКУПКУ + ФИЛЬТРЫ
        if btc == 0 and fast > slow:
            if price < trend:  # против тренда не торгуем
                skipped_signals += 1
                continue
            if r > RSI_MAX:    # перекуплено
                skipped_signals += 1
                continue
            spend = min(TRADE_AMOUNT, usdt)
            if spend <= 0:
                continue
            btc = spend * (1 - FEE) / price
            usdt -= spend
            last_spend = spend
            trades.append((times[i], 'BUY', price, None, r))
            last_trade_i = i
        # СИГНАЛ НА ПРОДАЖУ
        elif btc > 0 and fast < slow:
            got = btc * price * (1 - FEE)
            usdt += got
            pnl = got - last_spend
            btc = 0.0
            trades.append((times[i], 'SELL', price, pnl, r))
            last_trade_i = i

    equity = usdt + (btc * closes[-1] * (1 - FEE) if btc > 0 else 0.0)
    sells = [t for t in trades if t[1] == 'SELL']
    wins = [t for t in sells if t[3] and t[3] >= 0]
    buyhold = START_USDT * (1 - FEE) / closes[0] * closes[-1] * (1 - FEE)

    print("=" * 60)
    print(f"БЭКТЕСТ v2 (С ФИЛЬТРАМИ): {DAYS} дн. | {INTERVAL} | свечей: {len(closes)}")
    print(f"Стратегия MA{FAST_MA}/MA{SLOW_MA} + MA{TREND_MA}-тренд + RSI<{RSI_MAX}")
    print(f"Покупка {TRADE_AMOUNT} USDT | комиссия {FEE * 100}%")
    print("-" * 60)
    print(f"Сигналов отфильтровано: {skipped_signals}")
    print(f"Сделок всего: {len(trades)} (покупок {len(trades) - len(sells)}, продаж {len(sells)})")
    if sells:
        print(f"Прибыльных продаж: {len(wins)} из {len(sells)} ({len(wins) / len(sells) * 100:.0f}%)")
        total_pnl = sum(t[3] for t in sells if t[3])
        print(f"Суммарный P&L сделок: {total_pnl:+.2f} USDT")
    print(f"Итоговый депозит: {equity:.2f} USDT (старт {START_USDT:.2f})")
    print(f"P&L стратегии:  {equity - START_USDT:+.2f} USDT ({(equity / START_USDT - 1) * 100:+.2f}%)")
    print(f"P&L buy&hold:   {buyhold - START_USDT:+.2f} USDT ({(buyhold / START_USDT - 1) * 100:+.2f}%)")
    print("-" * 60)
    print("Последние 10 сделок:")
    for t in trades[-10:]:
        ts = datetime.datetime.fromtimestamp(t[0] / 1000).strftime('%d.%m %H:%M')
        pnl = f" | P&L {t[3]:+.2f}" if t[3] is not None else ""
        rsi_val = f"RSI={t[4]:.0f}" if t[4] else ""
        print(f"  {ts}  {t[1]:4s} @ {t[2]:,.2f} ({rsi_val}){pnl}")


if __name__ == "__main__":
    main()