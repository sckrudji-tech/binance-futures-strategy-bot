import asyncio
import aiohttp
import logging
import pandas as pd
import pandas_ta as ta
# from config import TELEGRAM_TOKEN, CHAT_ID # <--- ВИДАЛЕНО: Telegram тут не потрібен
# from telegram import Bot # <--- ВИДАЛЕНО

# Налаштування логування
logger = logging.getLogger('trend_analysis')
handler = logging.FileHandler('trend_analysis.log', encoding='utf-8')
handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] trend_analysis: %(message)s'))
logger.addHandler(handler)
logger.addHandler(logging.StreamHandler())  # Вивід у консоль
logger.setLevel(logging.DEBUG)

class TrendAnalysis:
    def __init__(self):
        self.session = None
        self.base_url = "https://testnet.binancefuture.com"

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        logger.info("Client session initialized")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def fetch_klines(self, symbol, interval='4h', limit=200): # <--- ЗМІНА: 4h
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
                logger.info(f"Fetched {len(df)} klines for {symbol} on {interval}")
                return df
        except Exception as e:
            logger.error(f"[{symbol}] Error fetching klines: {str(e)}")
            return None

    async def analyze_trend(self, symbol):
        """Аналіз трендових сигналів: EMA 50/200 Crossover + ADX (4h)."""
        try:
            df = await self.fetch_klines(symbol, interval='4h')
            if df is None or len(df) < 200:
                logger.warning(f"[{symbol}] Недостатньо даних для трендового аналізу (4h)")
                return None

            # Обчислення індикаторів
            try:
                df['ema_fast'] = ta.ema(df['close'], length=50) # <--- ЗМІНА: 50
                df['ema_slow'] = ta.ema(df['close'], length=200) # <--- ЗМІНА: 200
                df['adx'] = ta.adx(df['high'], df['low'], df['close'])['ADX_14']
                df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)

                if df['ema_fast'].isna().all() or df['ema_slow'].isna().all() or df['adx'].isna().all() or df['atr'].isna().all():
                    logger.warning(f"[{symbol}] Індикатори містять NaN значення")
                    return None
            except Exception as e:
                logger.error(f"[{symbol}] Помилка в обчисленні індикаторів: {str(e)}")
                return None

            current = df.iloc[-1]
            prev = df.iloc[-2]
            price = current['close']
            ema_fast = current['ema_fast']
            ema_slow = current['ema_slow']
            adx_value = current['adx']
            atr_at_entry = current['atr']

            signal = None
            is_strong_trend = adx_value > 25

            # Умова BUY: EMA 50 перетинає EMA 200 знизу вгору + ADX сильний
            if (ema_fast > ema_slow and prev['ema_fast'] <= prev['ema_slow']) and is_strong_trend:
                signal = 'buy'
            # Умова SELL: EMA 50 перетинає EMA 200 зверху вниз + ADX сильний
            elif (ema_fast < ema_slow and prev['ema_fast'] >= prev['ema_slow']) and is_strong_trend:
                signal = 'sell'

            if signal:
                logger.info(f"[{symbol}] Сигнал: {signal}, Ціна: {price:.4f}, EMA 50: {ema_fast:.4f}, EMA 200: {ema_slow:.4f}, ADX: {adx_value:.1f}, ATR: {atr_at_entry:.4f}")
                # <--- ВИЛУЧЕНО sl_price та send_telegram --->
                return {
                    'signal': signal, 
                    'price': price, 
                    'atr_at_entry': atr_at_entry, # <--- ДОДАНО ATR ДЛЯ TPSLManager
                    'ema_fast': ema_fast,
                    'ema_slow': ema_slow,
                    'adx_value': adx_value
                }
            return None
        except Exception as e:
            logger.error(f"[{symbol}] Помилка в трендовому аналізі: {str(e)}")
            return None

    # async def send_telegram(self, message): # <--- ВИДАЛЕНО
    #     """Відправка повідомлення в Telegram."""
    #     ...