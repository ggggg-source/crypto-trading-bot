import os
import asyncio
import logging
import hashlib
import hmac
import math
import time
import datetime
import json
import aiohttp
import requests
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.client.session.aiohttp import AiohttpSession

logging.basicConfig(level=logging.INFO)


# ⬇️ ТВОЙ ТОКЕН ОТ BOTFATHER
BOT_TOKEN = "-"
# ⬇️ ТВОИ КЛЮЧИ ОТ BINANCE TESTNET
API_KEY = "-"
API_SECRET = "-"

BINANCE_TESTNET = "https://testnet.binance.vision"
BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
KLINES_URL = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=250"

# --- ФАЙЛЫ ЖУРНАЛА И СОСТОЯНИЯ ---
CHAT_ID_FILE = "chat_id.txt"
TRADES_FILE = "trades.json"
STATE_FILE = "state.json"

# --- НАСТРОЙКИ СТРАТЕГИИ ---
TRADE_AMOUNT = 1000.0
RSI_PERIOD = 14
RSI_BUY = 30
RSI_SELL = 75
USE_TREND_FILTER = True
TREND_MA = 200
CHECK_EVERY = 300
MAX_DAILY_TRADES = 20
MAX_DAILY_LOSS = 200.0

session = AiohttpSession()
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()

# --- СОСТОЯНИЕ ---
auto_trading = False
chat_id = None
last_spend = 0.0  # себестоимость текущей позиции (USDT)
daily = {'date': '', 'trades': 0, 'pnl': 0.0, 'notified': False}


# --- РАБОТА С ФАЙЛАМИ ---
def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_trades():
    return load_json(TRADES_FILE, [])


def add_trade(trade):
    trades = load_trades()
    trades.append(trade)
    save_json(TRADES_FILE, trades)


def load_state():
    return load_json(STATE_FILE, {})


def save_state():
    save_json(STATE_FILE, {'last_spend': last_spend})


def load_chat_id():
    global chat_id
    if os.path.exists(CHAT_ID_FILE):
        try:
            with open(CHAT_ID_FILE) as f:
                chat_id = int(f.read().strip())
        except Exception:
            chat_id = None


def save_chat_id(cid):
    global chat_id
    chat_id = cid
    with open(CHAT_ID_FILE, 'w') as f:
        f.write(str(cid))


def reset_daily_if_needed():
    today = datetime.date.today().isoformat()
    if daily['date'] != today:
        daily.update({'date': today, 'trades': 0, 'pnl': 0.0, 'notified': False})


async def notify(text):
    if chat_id:
        try:
            await bot.send_message(chat_id, text)
        except Exception as e:
            logging.error(f"notify error: {e}")


# --- БИРЖА ---
def signed_request(method, path, params=None):
    params = params or {}
    params['recvWindow'] = 10000
    params['timestamp'] = int(time.time() * 1000)
    query = '&'.join(f"{k}={v}" for k, v in params.items())
    signature = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{BINANCE_TESTNET}{path}?{query}&signature={signature}"
    headers = {'X-MBX-APIKEY': API_KEY}
    resp = requests.request(method, url, headers=headers, timeout=20)
    if resp.status_code != 200:
        raise Exception(f"Binance {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def fetch_balance():
    data = signed_request('GET', '/api/v3/account')
    balances = {b['asset']: float(b['free']) for b in data['balances']}
    return balances.get('USDT', 0.0), balances.get('BTC', 0.0)


def buy_btc(cost):
    return signed_request('POST', '/api/v3/order', {
        'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quoteOrderQty': cost,
    })


def sell_btc_qty(qty):
    qty = math.floor(qty * 100000) / 100000
    if qty <= 0:
        raise Exception("Нет BTC для продажи")
    return signed_request('POST', '/api/v3/order', {
        'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': f"{qty:.5f}",
    })


# --- УЧЁТ ПОЗИЦИИ (себестоимость) ---
def avg_price(order):
    q = float(order.get('executedQty', 0))
    c = float(order.get('cummulativeQuoteQty', 0))
    return c / q if q > 0 else 0.0


def open_buy_accounting(order):
    """Увеличивает себестоимость позиции при покупке"""
    global last_spend
    spent = float(order.get('cummulativeQuoteQty', 0))
    last_spend += spent
    save_state()
    return spent


def close_sell_accounting(btc_before, order):
    """Считает P&L при продаже (работает и для частичной)"""
    global last_spend
    got = float(order.get('cummulativeQuoteQty', 0))
    sold = float(order.get('executedQty', 0))
    fraction = sold / btc_before if btc_before > 0 else 1.0
    cost_part = last_spend * fraction
    pnl = got - cost_part
    last_spend = max(0.0, last_spend - cost_part)
    save_state()
    return pnl, got, sold


def journal(order, side, pnl=None, rsi=None, source='auto'):
    add_trade({
        'ts': int(time.time()),
        'time': datetime.datetime.now().strftime('%d.%m %H:%M'),
        'side': side,
        'price': round(avg_price(order), 2),
        'btc': float(order.get('executedQty', 0)),
        'usdt': round(float(order.get('cummulativeQuoteQty', 0)), 2),
        'pnl': round(pnl, 2) if pnl is not None else None,
        'rsi': round(rsi, 1) if rsi is not None else None,
        'src': source,
    })


# --- ИНДИКАТОРЫ ---
async def get_candles():
    async with aiohttp.ClientSession() as s:
        async with s.get(KLINES_URL) as resp:
            data = await resp.json()
    return [float(c[4]) for c in data]


def ma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(diff, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-diff, 0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


# --- СТРАТЕГИЯ ---
async def strategy_step():
    global auto_trading
    reset_daily_if_needed()

    if daily['trades'] >= MAX_DAILY_TRADES:
        if not daily['notified']:
            daily['notified'] = True
            await notify(f"🛑 Лимит сделок на день ({MAX_DAILY_TRADES}). Пауза до завтра.")
        return

    closes = await get_candles()
    rsi = calculate_rsi(closes, RSI_PERIOD)
    trend = ma(closes, TREND_MA)
    price = closes[-1]
    if rsi is None or trend is None:
        return

    usdt, btc = await asyncio.to_thread(fetch_balance)
    has_position = btc > 0.0001

    if not has_position:
        if rsi < RSI_BUY:
            if USE_TREND_FILTER and price < trend:
                logging.info(f"Покупка отфильтрована: {price:.2f} < MA200 {trend:.2f}")
                return
            spend = min(TRADE_AMOUNT, usdt)
            if spend <= 0:
                return
            order = await asyncio.to_thread(buy_btc, spend)
            open_buy_accounting(order)
            journal(order, 'BUY', rsi=rsi, source='auto')
            daily['trades'] += 1
            await notify(f"🤖 АВТО-ПОКУПКА #{daily['trades']}\nRSI={rsi:.1f} (< {RSI_BUY})\nЦена: {price:,.2f} | MA200: {trend:,.2f}\nКуплено {float(order.get('executedQty', 0)):.6f} BTC за {float(order.get('cummulativeQuoteQty', 0)):.2f} USDT")
    else:
        if rsi > RSI_SELL:
            order = await asyncio.to_thread(sell_btc_qty, btc)
            pnl, got, sold = close_sell_accounting(btc, order)
            journal(order, 'SELL', pnl=pnl, rsi=rsi, source='auto')
            daily['trades'] += 1
            daily['pnl'] += pnl
            emoji = "🟢" if pnl >= 0 else "🔴"
            msg = (f"🤖📉 АВТО-ПРОДАЖА #{daily['trades']}\nRSI={rsi:.1f} (> {RSI_SELL})\n"
                   f"Продано {sold:.6f} BTC за {got:.2f} USDT\n"
                   f"{emoji} P&L сделки: {pnl:+.2f} USDT | За день: {daily['pnl']:+.2f} USDT")
            if daily['pnl'] <= -MAX_DAILY_LOSS:
                auto_trading = False
                msg += f"\n🛑 Лимит убытка ({MAX_DAILY_LOSS} USDT) — автопилот ВЫКЛЮЧЕН."
            await notify(msg)


async def strategy_loop():
    while True:
        try:
            if auto_trading:
                await strategy_step()
        except Exception as e:
            logging.error(f"strategy loop error: {e}")
        await asyncio.sleep(CHECK_EVERY)


# --- КОМАНДЫ ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    save_chat_id(message.from_user.id)
    await message.answer(
        "Привет! 🚀 Я твой алго-бот.\n\n"
        "📌 Ручные команды:\n"
        "/price — цена BTC\n"
        "/balance — баланс тестнета\n"
        "/buy 250 — купить на 250 USDT\n"
        "/sell 0.0015 — продать 0.0015 BTC\n"
        "/history — журнал сделок\n\n"
        "🤖 Автопилот (Mean Reversion):\n"
        f"RSI < {RSI_BUY} → покупка | RSI > {RSI_SELL} → продажа\n"
        "/auto on — включить\n"
        "/auto off — выключить\n"
        "/limits — лимиты и статистика"
    )


@dp.message(Command("price"))
async def cmd_price(message: types.Message):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(BINANCE_PRICE_URL) as resp:
                data = await resp.json()
        await message.answer(f"📈 BTC/USDT (Binance): {float(data['price']):,.2f} $")
    except Exception as e:
        await message.answer(f"😢 Ошибка цены: {e}")


@dp.message(Command("balance"))
async def cmd_balance(message: types.Message):
    try:
        usdt, btc = await asyncio.to_thread(fetch_balance)
        await message.answer(f"💰 Баланс тестнета:\n{usdt:.2f} USDT\n{btc:.6f} BTC")
    except Exception as e:
        await message.answer(f"😢 Ошибка баланса: {e}")


@dp.message(Command("buy"))
async def cmd_buy(message: types.Message, command: CommandObject):
    try:
        cost = float(command.args) if command.args else 100.0
    except ValueError:
        await message.answer("❌ Формат: /buy 250 (число в USDT)")
        return
    if cost <= 0:
        await message.answer("❌ Сумма должна быть больше 0")
        return
    try:
        await message.answer(f"🛒 Покупаю BTC на {cost} USDT...")
        order = await asyncio.to_thread(buy_btc, cost)
        open_buy_accounting(order)
        journal(order, 'BUY', source='manual')
        await message.answer(f"✅ Куплено: {float(order.get('executedQty', 0)):.6f} BTC\nПотрачено: {float(order.get('cummulativeQuoteQty', 0)):.2f} USDT")
    except Exception as e:
        await message.answer(f"😢 Ошибка сделки: {e}")


@dp.message(Command("sell"))
async def cmd_sell(message: types.Message, command: CommandObject):
    try:
        _, btc_before = await asyncio.to_thread(fetch_balance)
        qty = float(command.args) if command.args else btc_before
    except ValueError:
        await message.answer("❌ Формат: /sell 0.0015 (количество BTC)")
        return
    if qty <= 0 or btc_before <= 0:
        await message.answer("❌ Нет BTC для продажи")
        return
    try:
        await message.answer(f"📉 Продаю {qty:.6f} BTC...")
        order = await asyncio.to_thread(sell_btc_qty, qty)
        pnl, got, sold = close_sell_accounting(btc_before, order)
        journal(order, 'SELL', pnl=pnl, source='manual')
        await message.answer(f"✅ Продано: {sold:.6f} BTC\nПолучено: {got:.2f} USDT\nP&L: {pnl:+.2f} USDT")
    except Exception as e:
        await message.answer(f"😢 Ошибка продажи: {e}")


@dp.message(Command("history"))
async def cmd_history(message: types.Message):
    trades = load_trades()
    if not trades:
        await message.answer("📭 Журнал пуст — сделок ещё не было.")
        return
    sells = [t for t in trades if t['side'] == 'SELL' and t.get('pnl') is not None]
    total_pnl = sum(t['pnl'] for t in sells)
    wins = [t for t in sells if t['pnl'] >= 0]
    lines = [f"📖 ЖУРНАЛ СДЕЛОК (всего {len(trades)})\n"]
    for t in trades[-8:]:
        pnl = f" | P&L {t['pnl']:+.2f}" if t.get('pnl') is not None else ""
        src = "🤖" if t.get('src') == 'auto' else "👤"
        lines.append(f"{src} {t['time']} {t['side']} {t['btc']:.6f} BTC @ {t['price']:,.2f}{pnl}")
    lines.append(f"\n💵 Реализованный P&L: {total_pnl:+.2f} USDT")
    if sells:
        lines.append(f"Winrate: {len(wins)}/{len(sells)} = {len(wins) / len(sells) * 100:.0f}%")
    await message.answer("\n".join(lines))


@dp.message(Command("auto"))
async def cmd_auto(message: types.Message, command: CommandObject):
    global auto_trading
    save_chat_id(message.from_user.id)
    arg = (command.args or "").strip().lower()
    if arg == "on":
        auto_trading = True
        await message.answer(
            f"🤖 АВТОПИЛОТ ВКЛЮЧЁН!\n\n"
            f"Стратегия: Mean Reversion\n"
            f"RSI < {RSI_BUY} → покупка\n"
            f"RSI > {RSI_SELL} → продажа\n"
            f"Фильтр тренда MA{TREND_MA}: {'ВКЛ' if USE_TREND_FILTER else 'ВЫКЛ'}\n"
            f"Размер позиции: {TRADE_AMOUNT} USDT\n"
            f"Проверка каждые {CHECK_EVERY} сек\n\n"
            f"О каждой сделке напишу сюда.\nВыключить: /auto off"
        )
    elif arg == "off":
        auto_trading = False
        await message.answer("🛑 АВТОПИЛОТ ВЫКЛЮЧЕН.")
    else:
        state = "ВКЛЮЧЁН 🟢" if auto_trading else "ВЫКЛЮЧЕН 🔴"
        await message.answer(f"ℹ️ Автопилот: {state}\nКоманды: /auto on, /auto off")


@dp.message(Command("limits"))
async def cmd_limits(message: types.Message):
    reset_daily_if_needed()
    await message.answer(
        f"📊 ЛИМИТЫ И СТАТИСТИКА\n\n"
        f"Стратегия: Mean Reversion (RSI {RSI_BUY}/{RSI_SELL})\n"
        f"Размер позиции: {TRADE_AMOUNT} USDT\n"
        f"Максимум сделок в день: {MAX_DAILY_TRADES}\n"
        f"Максимум убытка в день: {MAX_DAILY_LOSS} USDT\n\n"
        f"📅 Сегодня: сделок {daily['trades']}, P&L {daily['pnl']:+.2f} USDT"
    )


@dp.message()
async def echo(message: types.Message):
    await message.answer(f"Ты написал: {message.text}")


async def main():
    global last_spend
    load_chat_id()
    # Восстанавливаем состояние после перезапуска
    state = load_state()
    last_spend = state.get('last_spend', 0.0)
    print("✅ Бот запущен! Жду сообщений в Telegram...")
    asyncio.create_task(strategy_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())