import os
import time
import datetime
import requests

# Прокси Happ
PROXY_URL = "http://127.0.0.1:10809"
os.environ['HTTP_PROXY'] = PROXY_URL
os.environ['HTTPS_PROXY'] = PROXY_URL

# --- НАСТРОЙКИ BREAKOUT + ПИРАМИДИНГ ---
SYMBOL = 'BTCUSDT'
INTERVAL = '1h'
START_DATE = '2024-01-01'
END_DATE = '2026-08-08'
ENTRY_PERIOD = 40
EXIT_PERIOD = 5
USE_TREND_FILTER = True
TREND_MA = 200
TRADE_AMOUNT = 1000.0      # размер одного юнита

# 🆕 НАСТРОЙКИ ПИРАМИДИНГА
USE_PYRAMIDING = True      # включить/выключить
PYRAMID_STEP = 1         # % роста от последнего входа для добавки
MAX_UNITS = 3             # максимум юнитов в позиции

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


def main():
    candles = fetch_klines()
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]
    times = [int(c[0]) for c in candles]
    print(f"\n✅ Загружено свечей: {len(closes)}\n")

    usdt = START_USDT
    # Позиция теперь - список юнитов: [(цена_входа, btc_количество, время), ...]
    units = []
    trades = []          # история закрытых сделок
    pyramid_adds = 0     # счётчик добавок
    total_units_opened = 0

    start_i = max(ENTRY_PERIOD, TREND_MA if USE_TREND_FILTER else 0) + 1

    for i in range(start_i, len(closes)):
        price = closes[i]

        if not units:
            # НЕТ ПОЗИЦИИ - ищем сигнал на вход
            hh = max(highs[i - ENTRY_PERIOD:i])
            if price > hh:
                if USE_TREND_FILTER and price < ma(closes[:i + 1], TREND_MA):
                    continue
                spend = min(TRADE_AMOUNT, usdt)
                if spend <= 0:
                    continue
                btc_qty = spend * (1 - FEE) / price
                usdt -= spend
                units.append((price, btc_qty, times[i]))
                trades.append((times[i], 'BUY 1', price, None))
                total_units_opened += 1
        else:
            # ЕСТЬ ПОЗИЦИЯ
            last_entry = units[-1][0]  # цена последнего входа

            # 1. ПРОВЕРКА ВЫХОДА (пробой минимума)
            ll = min(lows[i - EXIT_PERIOD:i])
            if price < ll:
                # продаём ВСЕ юниты разом
                total_btc = sum(u[1] for u in units)
                total_spent_sum = sum(u[0] * u[1] / (1 - FEE) for u in units)
                got = total_btc * price * (1 - FEE)
                usdt += got
                pnl = got - total_spent_sum
                trades.append((times[i], f'SELL x{len(units)}', price, pnl))
                units = []
                continue

            # 2. ПРОВЕРКА ПИРАМИДИНГА
            if USE_PYRAMIDING and len(units) < MAX_UNITS:
                # цена должна вырасти на PYRAMID_STEP% от последней точки входа
                if price >= last_entry * (1 + PYRAMID_STEP / 100):
                    spend = min(TRADE_AMOUNT, usdt)
                    if spend > 0:
                        btc_qty = spend * (1 - FEE) / price
                        usdt -= spend
                        units.append((price, btc_qty, times[i]))
                        trades.append((times[i], f'ADD {len(units)}', price, None))
                        pyramid_adds += 1
                        total_units_opened += 1

    # Если позиция осталась открытой - оцениваем по последней цене
    total_btc = sum(u[1] for u in units)
    open_value = total_btc * closes[-1] * (1 - FEE) if units else 0
    total_spent_open = sum(u[0] * u[1] / (1 - FEE) for u in units) if units else 0
    equity = usdt + open_value
    open_pnl = open_value - total_spent_open if units else 0

    # Подсчёт статистики (только по ЗАКРЫТЫМ сделкам)
    sells = [t for t in trades if t[1].startswith('SELL')]
    wins = [t for t in sells if t[3] is not None and t[3] >= 0]
    losses = [t for t in sells if t[3] is not None and t[3] < 0]
    gross_win = sum(t[3] for t in wins)
    gross_loss = abs(sum(t[3] for t in losses))
    buyhold = START_USDT * (1 - FEE) / closes[0] * closes[-1] * (1 - FEE)

    # Макс. просадка
    max_dd = 0
    peak = START_USDT
    test_usdt = START_USDT
    test_units = []
    for i in range(start_i, len(closes)):
        price = closes[i]
        if not test_units:
            hh = max(highs[i - ENTRY_PERIOD:i])
            if price > hh and (not USE_TREND_FILTER or price >= ma(closes[:i + 1], TREND_MA)):
                spend = min(TRADE_AMOUNT, test_usdt)
                if spend > 0:
                    test_units.append((price, spend * (1 - FEE) / price))
        else:
            ll = min(lows[i - EXIT_PERIOD:i])
            if price < ll:
                got = sum(u[1] for u in test_units) * price * (1 - FEE)
                test_usdt += got
                test_units = []
            elif USE_PYRAMIDING and len(test_units) < MAX_UNITS:
                if price >= test_units[-1][0] * (1 + PYRAMID_STEP / 100):
                    spend = min(TRADE_AMOUNT, test_usdt)
                    if spend > 0:
                        test_units.append((price, spend * (1 - FEE) / price))
                        test_usdt -= spend
        current_eq = test_usdt + (sum(u[1] for u in test_units) * price if test_units else 0)
        if current_eq > peak:
            peak = current_eq
        dd = (peak - current_eq) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # Макс. юнитов в одной сделке
    max_units_in_trade = max((int(t[1].split('x')[-1]) for t in sells), default=1) if sells else 1

    print("=" * 65)
    print(f"БЭКТЕСТ BREAKOUT + ПИРАМИДИНГ: {START_DATE} → {END_DATE} | {INTERVAL}")
    print(f"Вход: max {ENTRY_PERIOD} | Выход: min {EXIT_PERIOD} | Тренд MA{TREND_MA}: {'ВКЛ' if USE_TREND_FILTER else 'ВЫКЛ'}")
    print(f"Пирамидинг: {'ВКЛ' if USE_PYRAMIDING else 'ВЫКЛ'} | шаг {PYRAMID_STEP}% | макс {MAX_UNITS} юнитов")
    print(f"Размер юнита: {TRADE_AMOUNT} USDT")
    print("-" * 65)
    print(f"Сделок всего: {len(sells)} (входов: {total_units_opened}, добавок: {pyramid_adds})")
    print(f"Макс. юнитов в одной сделке: {max_units_in_trade}")
    if sells:
        print(f"Winrate: {len(wins)}/{len(sells)} = {len(wins) / len(sells) * 100:.0f}%")
        if wins:
            print(f"Средняя прибыль: +{gross_win / len(wins):.2f} USDT")
        if losses:
            print(f"Средний убыток:  -{gross_loss / len(losses):.2f} USDT")
        if gross_loss > 0:
            print(f"Profit Factor: {gross_win / gross_loss:.2f}")
        best = max(sells, key=lambda t: t[3])
        print(f"Лучшая сделка: {best[3]:+.2f} USDT")
    print(f"Итоговый депозит: {equity:.2f} USDT (старт {START_USDT:.2f})")
    if units:
        print(f"Открытая позиция: {total_btc:.6f} BTC (P&L {open_pnl:+.2f} USDT)")
    print(f"P&L стратегии:  {equity - START_USDT:+.2f} USDT ({(equity / START_USDT - 1) * 100:+.2f}%)")
    print(f"P&L buy&hold:   {buyhold - START_USDT:+.2f} USDT ({(buyhold / START_USDT - 1) * 100:+.2f}%)")
    print(f"Макс. просадка: {max_dd:.2f}%")
    print("=" * 65)


if __name__ == "__main__":
    main()