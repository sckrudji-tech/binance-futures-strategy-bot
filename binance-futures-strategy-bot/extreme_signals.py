import asyncio
import aiohttp
import logging
import pandas as pd
import pandas_ta as ta # <--- ДОДАНО: для Stochastic RSI та ATR
from ta.volatility import BollingerBands

# Налаштування логування
logger = logging.getLogger('extreme_signals')
handler = logging.FileHandler('extreme_signals.log', encoding='utf-8')
handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] extreme_signals: %(message)s'))
logger.addHandler(handler)
logger.addHandler(logging.StreamHandler())  # Вивід у консоль
logger.setLevel(logging.DEBUG)

class ExtremeSignals:
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

    async def fetch_klines(self, symbol, interval='1h', limit=200):
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

    async def analyze_extreme(self, symbol):
        """Аналіз екстремальних сигналів: Bollinger Bands + StochRSI."""
        try:
            df = await self.fetch_klines(symbol)
            if df is None or len(df) < 50:
                logger.warning(f"[{symbol}] Недостатньо даних для аналізу")
                return None

            # Обчислення індикаторів
            try:
                # Bollinger Bands (20/2)
                bb = BollingerBands(close=df['close'], window=20, window_dev=2)
                df['bb_upper'] = bb.bollinger_hband()
                df['bb_lower'] = bb.bollinger_lband()
                
                # Stochastic RSI (3/3/14/14)
                stoch_rsi = ta.stochrsi(df['close'], length=14, rsi_length=14, k=3, d=3)
                df['stoch_k'] = stoch_rsi['STOCHRSIk_14_14_3_3']
                
                # ATR (для розрахунку ризику)
                df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)

                if df['bb_upper'].isna().all() or df['bb_lower'].isna().all() or df['stoch_k'].isna().all() or df['atr'].isna().all():
                    logger.warning(f"[{symbol}] Індикатори містять NaN значення")
                    return None
            except Exception as e:
                logger.error(f"[{symbol}] Помилка в обчисленні індикаторів: {str(e)}")
                return None

            latest = df.iloc[-1]
            price = latest['close']
            bb_upper = latest['bb_upper']
            bb_lower = latest['bb_lower']
            stoch_k = latest['stoch_k']
            atr_at_entry = latest['atr']

            signal = None
            
            # Умова BUY: Закриття нижче BB_lower AND StochRSI < 20 (перепроданість)
            if (price < bb_lower) and (stoch_k < 20):
                signal = 'buy'
            # Умова SELL: Закриття вище BB_upper AND StochRSI > 80 (перекупленість)
            elif (price > bb_upper) and (stoch_k > 80):
                signal = 'sell'

            if signal:
                logger.info(f"[{symbol}] Сигнал: {signal}, Ціна: {price:.4f}, BB Upper: {bb_upper:.4f}, BB Lower: {bb_lower:.4f}, StochRSI: {stoch_k:.2f}, ATR: {atr_at_entry:.4f}")
                # <--- ВИЛУЧЕНО sl_price --->
                return {
                    'signal': signal, 
                    'price': price, 
                    'atr_at_entry': atr_at_entry, # <--- ДОДАНО ATR ДЛЯ TPSLManager
                    'bb_upper': bb_upper,
                    'bb_lower': bb_lower,
                    'stoch_k': stoch_k
                }
            return None
        except Exception as e:
            logger.error(f"[{symbol}] Помилка в аналізі екстремальних сигналів: {str(e)}")
            return None