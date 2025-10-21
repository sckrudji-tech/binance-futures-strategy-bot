import asyncio
import logging
import json 
from real_trading import RealTrader
from impulse_signals import ImpulseSignals
from extreme_signals import ExtremeSignals
from trend_analysis import TrendAnalysis
from tp_sl_manager import TPSLManager

# Налаштування логування для StrategyManager
logger = logging.getLogger('strategy_manager')
handler = logging.FileHandler('strategy_manager.log', encoding='utf-8')
handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] StrategyManager: %(message)s'))
logger.addHandler(handler)
logger.addHandler(logging.StreamHandler())  # Вивід у консоль
logger.setLevel(logging.DEBUG)

# --- НАЛАШТУВАННЯ ЛОГУВАННЯ ДЛЯ ОРДЕРІВ (trades.log) ---
trade_logger = logging.getLogger('trade_journal')
trade_handler = logging.FileHandler('trades.log', encoding='utf-8')
trade_handler.setFormatter(logging.Formatter('%(asctime)s: %(message)s')) 
trade_logger.addHandler(trade_handler)
trade_logger.setLevel(logging.INFO)
# ----------------------------------------------------------------

async def manage_strategy(symbol, trader, signal_analyzer, strategy_name):
    """Управління стратегіями для одного символу."""
    try:
        open_positions = trader.get_open_positions()
        symbol_positions = [pos for pos in open_positions if pos['symbol'] == symbol]
        
        if len(symbol_positions) > 0:
            logger.info(f"[{symbol}] Пропускаємо аналіз для {strategy_name}: уже є відкрита позиція")
            return
        
        # Аналіз сигналу залежно від типу стратегії
        if strategy_name == 'impulse':
            signal_data = await signal_analyzer.analyze_impulse(symbol)
        elif strategy_name == 'extreme':
            signal_data = await signal_analyzer.analyze_extreme(symbol)
        else:  # trend
            signal_data = await signal_analyzer.analyze_trend(symbol)
        
        if signal_data and 'signal' in signal_data:
            signal = signal_data['signal']
            price = signal_data['price']
            atr_at_entry = signal_data.get('atr_at_entry', 0.0) # Отримуємо ATR
            
            if atr_at_entry <= 0:
                logger.warning(f"[{symbol}] {strategy_name} не надав коректний ATR, пропускаємо.")
                return

            logger.info(f"[{symbol}] {strategy_name} сигнал: {signal}, Ціна: {price:.4f}, ATR: {atr_at_entry:.4f}")
            
            side = 'long' if signal == 'buy' else 'short'
            
            # Виділяємо деталі індикаторів
            indicator_details = {k: f"{v:.4f}" if isinstance(v, (int, float)) else v for k, v in signal_data.items() if k not in ['signal', 'price', 'atr_at_entry']}
            
            # <--- ЗМІНА: Передаємо atr_at_entry замість sl_price --->
            order_id = await trader.open_position(symbol, side, price, atr_at_entry)
            
            if order_id:
                logger.info(f"[{symbol}] Відкрито позицію для {strategy_name}: {side.upper()} ID {order_id}")
                
                # --- ЛОГУВАННЯ ВІДКРИТТЯ ОРДЕРА У trades.log ---
                trade_record = {
                    'action': 'OPEN',
                    'order_id': order_id,
                    'symbol': symbol,
                    'strategy': strategy_name,
                    'side': side.upper(),
                    'open_price': price,
                    # 'stop_loss': sl_price, # <--- ВИДАЛЕНО
                    'atr_at_entry': atr_at_entry, # <--- ДОДАНО: ATR для логування
                    'signal_details': indicator_details 
                }
                # Записуємо JSON-структуру в один рядок
                trade_logger.info(json.dumps(trade_record))
                # ----------------------------------------------------
                
    except Exception as e:
        logger.error(f"[{symbol}] Помилка в {strategy_name} стратегії: {str(e)}")

async def main():
    logger.info("Запуск бота...")
    
    # ... (ініціалізація RealTrader та аналізаторів)
    async with RealTrader(testnet=True, leverage=10) as impulse_trader, \
               RealTrader(testnet=True, leverage=10) as extreme_trader, \
               RealTrader(testnet=True, leverage=10) as trend_trader, \
               ImpulseSignals() as impulse_analyzer, \
               ExtremeSignals() as extreme_analyzer, \
               TrendAnalysis() as trend_analyzer, \
               TPSLManager(trade_logger) as tp_sl_manager:
        
        # ... (перевірки підключення та отримання топ пар)
        
        # Перевірка підключення до API (для стислості коду опущено, вважаємо, що вони працюють)
        # ...

        top_pairs = await impulse_analyzer.get_top_pairs(limit=40)
        if not top_pairs:
            logger.error("Не вдалося отримати топ пар")
            return
        
        logger.info(f"Аналіз {len(top_pairs)} топ пар...")
        
        while True:
            try:
                # Завдання для аналізу всіх пар
                impulse_tasks = [manage_strategy(symbol, impulse_trader, impulse_analyzer, 'impulse') for symbol in top_pairs]
                extreme_tasks = [manage_strategy(symbol, extreme_trader, extreme_analyzer, 'extreme') for symbol in top_pairs]
                trend_tasks = [manage_strategy(symbol, trend_trader, trend_analyzer, 'trend') for symbol in top_pairs]
                
                # Завдання для управління TP/SL
                tp_sl_tasks = []
                for trader, strategy_name in [(impulse_trader, 'impulse'), (extreme_trader, 'extreme'), (trend_trader, 'trend')]:
                    open_positions = trader.get_open_positions()
                    for pos in open_positions:
                        # Додаємо strategy_name та ATR до позиції
                        pos['strategy_name'] = strategy_name 
                        # pos['atr_at_entry'] вже додано в RealTrader.open_position
                        tp_sl_tasks.append(tp_sl_manager.check_tp_sl(trader, pos))
                
                # Виконання всіх завдань
                all_tasks = impulse_tasks + extreme_tasks + trend_tasks + tp_sl_tasks
                await asyncio.gather(*all_tasks)
                
                # Затримка перед наступним циклом
                await asyncio.sleep(60)  # 1 хвилина
                
            except Exception as e:
                logger.error(f"Помилка в основному циклі: {str(e)}")
                await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())