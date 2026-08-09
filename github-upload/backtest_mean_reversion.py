import os
import time
import datetime
import requests

# Прокси Happ
PROXY_URL = "http://127.0.0.1:10809"
os.environ['HTTP_PROXY'] = PROXY_URL
os.environ['HTTPS_PROXY'] = PROXY_URL

# --- НАСТРОЙКИ MEAN REVERSION ---
SYMBOL = 'BTCUSDT'
INTERVAL = '1h'
START_DATE = '2026-01-01'
END_DATE = '2026-08-08'

# Параметры RSI
RSI_PERIOD = 14
RSI_BUY = 30        # покупаем когда RSI ниже этого
RSI_SELL = 75       # продаём когда RSI выше этого

# Фильтр тренда (опционально)
USE_TREND_FILTER = True  # пока выключаем - тестируем чистую стратегию
TREND_MA = 200

TRADE_AMOUNT = 1000.0
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
        print(f"   загружено свечей: {len(candles)}")
        time.sleep(0.3)
    return candles


def ma(vals, p):
    return sum(vals[-p:]) / p


def calculate_rsi(closes, period=14):
    """Рассчитывает RSI для всего массива цен"""
    if len(closes) < period + 1:
        return []
    
    rsi_values = [None] * period  # первые значения не можем посчитать
    
    # Первый средний gain/loss
    gains = []
    losses = []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    
    if avg_loss == 0:
        rsi_values.append(100.0)
    else:
        rs = avg_gain / avg_loss
        rsi_values.append(100.0 - 100.0 / (1.0 + rs))
    
    # Остальные значения используем сглаживание
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = max(diff, 0)
        loss = max(-diff, 0)
        
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        
        if avg_loss == 0:
            rsi_values.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_values.append(100.0 - 100.0 / (1.0 + rs))
    
    return rsi_values


def main():
    candles = fetch_klines()
    closes = [float(c[4]) for c in candles]
    times = [int(c[0]) for c in candles]
    
    print(f"\n✅ Загружено свечей: {len(closes)}")
    print(f"🔄 Рассчитываю RSI({RSI_PERIOD})...")
    
    rsi_values = calculate_rsi(closes, RSI_PERIOD)
    print(f"✅ RSI рассчитан\n")

    usdt = START_USDT
    btc = 0.0
    last_spend = 0.0
    trades = []

    start_i = max(RSI_PERIOD + 1, TREND_MA if USE_TREND_FILTER else 0)

    for i in range(start_i, len(closes)):
        price = closes[i]
        rsi = rsi_values[i]
        
        if rsi is None:
            continue

        if btc == 0:
            # НЕТ ПОЗИЦИИ - проверяем сигнал на покупку
            if rsi < RSI_BUY:
                if USE_TREND_FILTER and price < ma(closes[:i + 1], TREND_MA):
                    continue
                spend = min(TRADE_AMOUNT, usdt)
                if spend <= 0:
                    continue
                btc = spend * (1 - FEE) / price
                usdt -= spend
                last_spend = spend
                trades.append((times[i], 'BUY', price, rsi, None))
        else:
            # ЕСТЬ ПОЗИЦИЯ - проверяем сигнал на продажу
            if rsi > RSI_SELL:
                got = btc * price * (1 - FEE)
                usdt += got
                pnl = got - last_spend
                btc = 0.0
                trades.append((times[i], 'SELL', price, rsi, pnl))

    # Если позиция осталась открытой - оцениваем по последней цене
    equity = usdt + (btc * closes[-1] * (1 - FEE) if btc > 0 else 0.0)
    sells = [t for t in trades if t[1] == 'SELL']
    wins = [t for t in sells if t[4] is not None and t[4] >= 0]
    losses = [t for t in sells if t[4] is not None and t[4] < 0]
    gross_win = sum(t[4] for t in wins)
    gross_loss = abs(sum(t[4] for t in losses))
    buyhold = START_USDT * (1 - FEE) / closes[0] * closes[-1] * (1 - FEE)

    # Макс. просадка
    max_dd = 0
    peak = START_USDT
    test_usdt = START_USDT
    test_btc = 0.0
    for i in range(start_i, len(closes)):
        price = closes[i]
        rsi = rsi_values[i]
        if rsi is None:
            continue
        
        if test_btc == 0:
            if rsi < RSI_BUY and (not USE_TREND_FILTER or price >= ma(closes[:i + 1], TREND_MA)):
                spend = min(TRADE_AMOUNT, test_usdt)
                if spend > 0:
                    test_btc = spend * (1 - FEE) / price
                    test_usdt -= spend
        else:
            if rsi > RSI_SELL:
                got = test_btc * price * (1 - FEE)
                test_usdt += got
                test_btc = 0.0
        
        current_eq = test_usdt + (test_btc * price if test_btc > 0 else 0)
        if current_eq > peak:
            peak = current_eq
        dd = (peak - current_eq) / peak * 100
        if dd > max_dd:
            max_dd = dd

    print("=" * 65)
    print(f"БЭКТЕСТ MEAN REVERSION: {START_DATE} → {END_DATE} | {INTERVAL}")
    print(f"RSI({RSI_PERIOD}) | Покупка при RSI < {RSI_BUY} | Продажа при RSI > {RSI_SELL}")
    print(f"Фильтр тренда MA{TREND_MA}: {'ВКЛ' if USE_TREND_FILTER else 'ВЫКЛ'}")
    print(f"Размер позиции: {TRADE_AMOUNT} USDT")
    print("-" * 65)
    print(f"Сделок всего: {len(sells)} (покупок {len(trades) - len(sells)}, продаж {len(sells)})")
    if sells:
        print(f"Winrate: {len(wins)}/{len(sells)} = {len(wins) / len(sells) * 100:.0f}%")
        if wins:
            print(f"Средняя прибыль: +{gross_win / len(wins):.2f} USDT")
        if losses:
            print(f"Средний убыток:  -{gross_loss / len(losses):.2f} USDT")
        if gross_loss > 0:
            print(f"Profit Factor: {gross_win / gross_loss:.2f}")
        best = max(sells, key=lambda t: t[4])
        worst = min(sells, key=lambda t: t[4])
        print(f"Лучшая сделка: {best[4]:+.2f} USDT (RSI={best[3]:.0f})")
        print(f"Худшая сделка: {worst[4]:+.2f} USDT (RSI={worst[3]:.0f})")
    print(f"Итоговый депозит: {equity:.2f} USDT (старт {START_USDT:.2f})")
    if btc > 0:
        open_pnl = btc * closes[-1] * (1 - FEE) - last_spend
        print(f"Открытая позиция: {btc:.6f} BTC (P&L {open_pnl:+.2f} USDT)")
    print(f"P&L стратегии:  {equity - START_USDT:+.2f} USDT ({(equity / START_USDT - 1) * 100:+.2f}%)")
    print(f"P&L buy&hold:   {buyhold - START_USDT:+.2f} USDT ({(buyhold / START_USDT - 1) * 100:+.2f}%)")
    print(f"Макс. просадка: {max_dd:.2f}%")
    print("=" * 65)


if __name__ == "__main__":
    main()