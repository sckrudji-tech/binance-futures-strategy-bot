import asyncio
import aiohttp
import logging
import pandas as pd
import pandas_ta as ta # Використовуємо pandas_ta замість ta для більшої універсальності
from ta.volume import VolumeWeightedAveragePrice
from datetime import datetime

# Налаштування логування
logger = logging.getLogger('impulse_signals')
handler = logging.FileHandler('impulse_signals.log', encoding='utf-8')
handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] impulse_signals: %(message)s'))
logger.addHandler(handler)
logger.addHandler(logging.StreamHandler())  # Вивід у консоль
logger.setLevel(logging.DEBUG)

class ImpulseSignals:
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

    async def get_top_pairs(self, limit=20):
        """Отримати топ торгових пар за обсягом."""
        try:
            logger.info(f"API request to {self.base_url}/fapi/v1/ticker/24hr with params None")
            async with self.session.get(f"{self.base_url}/fapi/v1/ticker/24hr") as response:
                if response.status != 200:
                    logger.error(f"Failed to fetch pairs: {response.status} - {await response.text()}")
                    return []
                data = await response.json()
                logger.info(f"Received data: {len(data)} items")
                
                pairs = [
                    item for item in data
                    if item['symbol'].endswith(('USDT', 'USDC')) and float(item['quoteVolume']) > 0
                ]
                pairs.sort(key=lambda x: float(x['quoteVolume']), reverse=True)
                top_pairs = [item['symbol'] for item in pairs[:limit]]
                logger.info(f"Fetched {len(top_pairs)} valid pairs: {top_pairs}")
                return top_pairs
        except Exception as e:
            logger.error(f"Error fetching top pairs: {str(e)}")
            return []

    async def fetch_klines(self, symbol, interval='15m', limit=200): # <--- ЗМІНА: 15m
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

    async def analyze_impulse(self, symbol):
        """Аналіз імпульсних сигналів: VWAP Crossover + RSI Momentum."""
        try:
            df = await self.fetch_klines(symbol, interval='15m')
            if df is None or len(df) < 50:
                logger.warning(f"[{symbol}] Недостатньо даних для аналізу")
                return None

            # Обчислення індикаторів
            try:
                # VWAP (стандартний період 14)
                vwap = VolumeWeightedAveragePrice(
                    high=df['high'], low=df['low'], close=df['close'], volume=df['volume'], window=14
                )
                df['vwap'] = vwap.volume_weighted_average_price()
                
                # RSI (короткий період 5 для імпульсу)
                df['rsi'] = ta.rsi(df['close'], length=5)
                
                # ATR (для розрахунку ризику)
                df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
                
                if df['vwap'].isna().all() or df['rsi'].isna().all() or df['atr'].isna().all():
                    logger.warning(f"[{symbol}] Індикатори містять NaN значення")
                    return None
            except Exception as e:
                logger.error(f"[{symbol}] Помилка в обчисленні індикаторів: {str(e)}")
                return None

            latest = df.iloc[-1]
            prev = df.iloc[-2]
            price = latest['close']
            vwap_value = latest['vwap']
            rsi_value = latest['rsi']
            atr_at_entry = latest['atr']

            signal = None
            
            # Умова BUY: Ціна перетнула VWAP знизу вгору + RSI підтверджує сильний імпульс
            if (price > vwap_value and prev['close'] <= prev['vwap']) and (rsi_value > 60):
                signal = 'buy'
            # Умова SELL: Ціна перетнула VWAP зверху вниз + RSI підтверджує сильний імпульс
            elif (price < vwap_value and prev['close'] >= prev['vwap']) and (rsi_value < 40):
                signal = 'sell'

            if signal:
                logger.info(f"[{symbol}] Сигнал: {signal}, Ціна: {price:.4f}, VWAP: {vwap_value:.4f}, RSI: {rsi_value:.2f}, ATR: {atr_at_entry:.4f}")
                # <--- ВИЛУЧЕНО sl_price --->
                return {
                    'signal': signal, 
                    'price': price, 
                    'atr_at_entry': atr_at_entry, # <--- ДОДАНО ATR ДЛЯ TPSLManager
                    'vwap_value': vwap_value,
                    'rsi_value': rsi_value
                }
            return None
        except Exception as e:
            logger.error(f"[{symbol}] Помилка в імпульсному аналізі: {str(e)}")
            return None