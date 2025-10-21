import asyncio
import aiohttp
import logging
import pandas as pd
import pandas_ta as ta
import json # <--- ДОДАНО

# Налаштування логування
logger = logging.getLogger('tp_sl_manager')
handler = logging.FileHandler('tp_sl_manager.log', encoding='utf-8')
handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] tp_sl_manager: %(message)s'))
logger.addHandler(handler)
logger.addHandler(logging.StreamHandler())  # Вивід у консоль
logger.setLevel(logging.DEBUG)

# Налаштування виходу
STRATEGY_TIMEFRAMES = {
    'impulse': '4h', # Виходимо на 1h, навіть якщо вхід на 15m, для зменшення шуму
    'extreme': '4h',
    'trend': '4h'
}
ADX_SIDEWAY_THRESHOLD = 20
ATR_SL_MULTIPLIER = 1.5 # Множник для початкового Стоп-Лоссу (Hard SL)
ATR_TP_MULTIPLIER = 2.5 # Множник для початкового Тейк-Профіту (Ціль 1:2 R:R)

class TPSLManager:
    # ОНОВЛЕНО: Приймаємо trade_logger
    def __init__(self, trade_logger):
        self.session = None
        self.base_url = "https://testnet.binancefuture.com"
        self.trade_logger = trade_logger 
        logger.info("TPSLManager initialized with Trade Journal logger")

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        logger.info("Client session initialized")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def fetch_klines(self, symbol, interval='1h', limit=100):
        """Отримати свічки для символу."""
        params = {'symbol': symbol, 'interval': interval, 'limit': limit}
        try:
            logger.info(f"API request to {self.base_url}/fapi/v1/klines with params {params}")
            async with self.session.get(f"{self.base_url}/fapi/v1/klines", params=params) as response:
                if response.status != 200:
                    logger.error(f"[{symbol}] Failed to fetch klines: {response.status} - {await response.text()}")
                    return None
                data = await response.json()
                logger.info(f"Received data: {len(data)} items")
                
                df = pd.DataFrame(data, columns=[
                    'open_time', 'open', 'high', 'low', 'close', 'volume',
                    'close_time', 'quote_volume', 'trades', 'taker_buy_volume',
                    'taker_buy_quote_volume', 'ignore'
                ])
                df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
                df.set_index('open_time', inplace=True)
                df = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
                logger.info(f"Fetched {len(df)} klines for {symbol}")
                return df
        except Exception as e:
            logger.error(f"[{symbol}] Error fetching klines: {str(e)}")
            return None

    async def check_tp_sl(self, trader, position):
        """Моніторинг та закриття позиції на основі Hard SL/TP, PSAR, MACD, ADX."""
        try:
            symbol = position['symbol']
            order_id = position['order_id']
            strategy_name = position.get('strategy_name', 'unknown')
            entry_price = position['entry_price']
            side = position['side']
            # atr_at_entry необхідний для початкового SL/TP
            atr_at_entry = position.get('atr_at_entry') 
            
            if atr_at_entry is None:
                logger.error(f"[{symbol}] Помилка: відсутній atr_at_entry для розрахунку ризику. Пропускаємо.")
                return

            timeframe = STRATEGY_TIMEFRAMES.get(strategy_name, '1h')
            df = await self.fetch_klines(symbol, timeframe, limit=100)
            if df is None or len(df) < 50:
                logger.warning(f"[{symbol}] Недостатньо даних для моніторингу (таймфрейм: {timeframe})")
                return

            # Обчислення індикаторів
            try:
                macd = ta.macd(df['close'])
                df['macd'] = macd['MACD_12_26_9']
                df['macd_signal'] = macd['MACDs_12_26_9']
                df['adx'] = ta.adx(df['high'], df['low'], df['close'])['ADX_14']
                # PSAR для трейлінгу (0.02, 0.2 - стандартні та ефективні)
                psar = ta.psar(df['high'], df['low'], df['close'])
                df['psar'] = psar['PSARr_0.02_0.2']
                
                if df['macd'].isna().all() or df['macd_signal'].isna().all() or df['adx'].isna().all() or df['psar'].isna().all():
                    logger.warning(f"[{symbol}] Індикатори містять NaN значення")
                    return
            except Exception as e:
                logger.error(f"[{symbol}] Помилка в обчисленні індикаторів: {str(e)}")
                return

            current = df.iloc[-1]
            prev = df.iloc[-2]
            current_price = current['close']
            reason_to_close = ""
            
            # --- ВИДІЛЕННЯ ІНДИКАТОРІВ НА МОМЕНТ ЗАКРИТТЯ ---
            close_indicator_details = {
                'macd': current['macd'],
                'macd_signal': current['macd_signal'],
                'adx': current['adx'],
                'psar': current['psar'],
            }
            # ------------------------------------------------

            # 1. Визначення Hard SL та TP на основі ATR при вході
            if side == 'long':
                sl_price_hard = entry_price - (atr_at_entry * ATR_SL_MULTIPLIER)
                tp_price_hard = entry_price + (atr_at_entry * ATR_TP_MULTIPLIER)
            else:
                sl_price_hard = entry_price + (atr_at_entry * ATR_SL_MULTIPLIER)
                tp_price_hard = entry_price - (atr_at_entry * ATR_TP_MULTIPLIER)

            is_long = side == 'long'

            # 2. Перевірка Hard Stop-Loss
            if (is_long and current_price <= sl_price_hard) or (not is_long and current_price >= sl_price_hard):
                reason_to_close = f"Досягнуто Hard Stop-Loss (${sl_price_hard:.4f}) по ATR"

            # 3. Перевірка Take-Profit
            if not reason_to_close:
                if (is_long and current_price >= tp_price_hard) or (not is_long and current_price <= tp_price_hard):
                    reason_to_close = f"Досягнуто Take-Profit (${tp_price_hard:.4f}) (R:R 1:{ATR_TP_MULTIPLIER / ATR_SL_MULTIPLIER:.1f})"

            # 4. Перевірка Trailing Stop (PSAR)
            if not reason_to_close:
                psar_value = current['psar']
                if (is_long and current_price < psar_value) or (not is_long and current_price > psar_value):
                    reason_to_close = f"Трейлінг-стоп по Parabolic SAR ({psar_value:.4f})"

            # 5. Перевірка Розвороту Тренда (MACD Crossover)
            if not reason_to_close:
                if (is_long and current['macd'] < current['macd_signal'] and prev['macd'] >= prev['macd_signal']) or \
                   (not is_long and current['macd'] > current['macd_signal'] and prev['macd'] <= prev['macd_signal']):
                    reason_to_close = "Розворот тренду (MACD Crossover)"

            # 6. Перевірка Боковика (ADX)
            if not reason_to_close and current['adx'] < ADX_SIDEWAY_THRESHOLD:
                reason_to_close = f"Ринок у боковику (ADX: {current['adx']:.1f})"

            if reason_to_close:
                close_data = await trader.close_position(order_id, current_price, reason_to_close) 
                
                pnl_percent = close_data.get('pnl_percent', 0.0) if close_data else 0.0
                pnl_amount = close_data.get('pnl_amount', 0.0) if close_data else 0.0
                close_time = close_data.get('close_time', pd.Timestamp.now().isoformat()) if close_data else pd.Timestamp.now().isoformat()
                
                logger.info(f"[{symbol}] Закрито позицію {side.upper()} (ID: {order_id}) за причиною: {reason_to_close}")
                
                # --- ЛОГУВАННЯ ЗАКРИТТЯ ОРДЕРА У trades.log ---
                trade_record = {
                    'action': 'CLOSE',
                    'order_id': order_id,
                    'symbol': symbol,
                    'strategy': strategy_name,
                    'side': side.upper(),
                    'close_price': current_price,
                    'close_time': close_time,
                    'pnl_percent': pnl_percent,
                    'pnl_amount': pnl_amount,
                    'reason_to_close': reason_to_close,
                    'indicator_details_at_close': {k: f"{v:.4f}" for k, v in close_indicator_details.items()} 
                }
                self.trade_logger.info(json.dumps(trade_record))
                # ----------------------------------------------------
                
        except Exception as e:
            logger.error(f"[{symbol}] Помилка в моніторингу TP/SL: {str(e)}")