# ws_manager.py
from binance import ThreadedWebsocketManager
import pandas as pd
import logging

class RealTimeData:
    def __init__(self):
        self.data = {}
        self.twm = ThreadedWebsocketManager()
        self.twm.start()
    
    def start_kline_socket(self, symbol: str, interval: str = '1m'):
        def handle_socket(msg):
            if msg['e'] == 'kline':
                kline = msg['k']
                df = pd.DataFrame([{
                    'timestamp': pd.to_datetime(kline['t'], unit='ms'),
                    'open': float(kline['o']),
                    'high': float(kline['h']),
                    'low': float(kline['l']),
                    'close': float(kline['c']),
                    'volume': float(kline['v'])
                }])
                self.data[symbol] = df
        
        self.twm.start_kline_socket(callback=handle_socket, symbol=symbol, interval=interval)