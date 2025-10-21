import asyncio
import logging
import hmac
import hashlib
import time
import urllib.parse
from datetime import datetime
import aiohttp
from config import API_KEY, API_SECRET, TELEGRAM_TOKEN, CHAT_ID
from telegram import Bot

# Налаштування логування
logger = logging.getLogger('real_trading')
handler = logging.FileHandler('real_trading.log', encoding='utf-8')
handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] RealTrading: %(message)s'))
logger.addHandler(handler)
logger.addHandler(logging.StreamHandler())  # Вивід у консоль
logger.setLevel(logging.DEBUG)

class RealTrader:
    # ЗМІНА 1: Встановлюємо фіксовану суму ризику як константу класу
    FIXED_RISK_AMOUNT = 100.0  # Фіксована сума ризику в доларах США
    DEFAULT_STOP_MULTIPLIER = 2.5 # <--- ДОДАНО: Множник ATR для розрахунку розміру позиції

    # НОВИЙ: Семфор для серіалізації signed запитів (max_concurrent=1 для уникнення rate limit)
    _api_semaphore = asyncio.Semaphore(1)

    # ЗМІНА 2: Видаляємо risk_per_trade з конструктора
    def __init__(self, testnet=True, leverage=10, commission=0.0004, log_file='real_trading.log'):
        self.leverage = leverage
        self.commission = commission
        self.positions = {}
        self.trade_history = []
        self.base_url = "https://testnet.binancefuture.com" if testnet else "https://fapi.binance.com"
        self.session = None
        self.server_time_diff = 0  # Різниця між локальним і серверним часом
        self.logger = logger
        self.initialized_symbols = set()  # Трекер ініціалізованих символів
        self.top_symbols = []  # Список топ символів
        self.logger.info(f"RealTrader ініціалізовано: testnet={testnet}, leverage={leverage}, фіксований ризик=${self.FIXED_RISK_AMOUNT}, ATR Stop Multiplier={self.DEFAULT_STOP_MULTIPLIER}")

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        await self.sync_server_time()  # Синхронізувати час при ініціалізації
        await self._fetch_top_symbols()  # Отримати топ символи
        await self.setup_all_symbols(self.top_symbols)  # Ініціалізувати всі символи при старті
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def _fetch_top_symbols(self, limit=40):
        """Отримати топ 20 символів за обсягом торгів (unsigned запит)."""
        try:
            async with self.session.get(f"{self.base_url}/fapi/v1/ticker/24hr") as response:
                if response.status == 200:
                    data = await response.json()
                    # Фільтруємо USDT/USDC пари, сортуємо за volume
                    usdt_pairs = [item for item in data if item['symbol'].endswith('USDT') or item['symbol'].endswith('USDC')]
                    sorted_pairs = sorted(usdt_pairs, key=lambda x: float(x['quoteVolume']), reverse=True)[:limit]
                    self.top_symbols = [pair['symbol'] for pair in sorted_pairs]
                    self.logger.info(f"Отриманно топ {limit} символів: {self.top_symbols}")
                    return True
                else:
                    self.logger.error("Не вдалося отримати топ символи")
                    return False
        except Exception as e:
            self.logger.error(f"Помилка отримання топ символів: {str(e)}")
            return False

    async def sync_server_time(self):
        """Синхронізувати час з сервером Binance."""
        try:
            async with self.session.get(f"{self.base_url}/fapi/v1/time", timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    server_time = data.get('serverTime', 0) / 1000  # У секундах
                    local_time = time.time()
                    self.server_time_diff = server_time - local_time
                    self.logger.info(f"Синхронізовано час сервера: різниця {self.server_time_diff:.3f} секунд")
                    return True
                else:
                    text = await response.text()
                    self.logger.error(f"Помилка синхронізації часу: {response.status} - {text}")
                    return False
        except Exception as e:
            self.logger.error(f"Помилка синхронізації часу: {str(e)}")
            return False

    async def _signed_request(self, method, endpoint, params=None, data=None, retries=15, delay=15, timeout=180):  
        """Оновлено: Збільшено retries, delay, timeout; експоненційний backoff."""
        async with self._api_semaphore:  # Серіалізуємо всі signed запити
            if params is None:
                params = {}
            if data is None:
                data = {}

            await self.sync_server_time()  # Синхронізувати час перед кожним запитом

            timestamp = int((time.time() + self.server_time_diff) * 1000)
            params['timestamp'] = timestamp

            # Обчислюємо рядок для підпису
            sign_string = urllib.parse.urlencode(sorted(params.items()))

            signature = hmac.new(API_SECRET.encode(), sign_string.encode(), hashlib.sha256).hexdigest()

            headers = {'X-MBX-APIKEY': API_KEY}

            url = f"{self.base_url}{endpoint}"

            if method == 'GET':
                params['signature'] = signature
                request_params = params
                request_data = None
            else:  # POST або DELETE
                # Для POST, надсилаємо параметри в body
                body_string = sign_string + '&signature=' + signature
                request_params = None
                request_data = body_string
                headers['Content-Type'] = 'application/x-www-form-urlencoded'

            for attempt in range(1, retries + 1):
                try:
                    self.logger.debug(f"Спроба {attempt}/{retries} для запиту {method} {endpoint}")
                    if method == 'GET':
                        async with self.session.get(url, params=request_params, headers=headers, timeout=timeout) as response:
                            return await self._handle_response(response, endpoint)
                    elif method == 'POST':
                        async with self.session.post(url, data=request_data, headers=headers, timeout=timeout) as response:
                            return await self._handle_response(response, endpoint)
                    elif method == 'DELETE':
                        async with self.session.delete(url, data=request_data, headers=headers, timeout=timeout) as response:
                            return await self._handle_response(response, endpoint)
                except Exception as e:
                    self.logger.error(f"Помилка запиту до {endpoint} (спроба {attempt}/{retries}): {str(e)}")
                    if attempt < retries:
                        backoff_delay = delay * (2 ** (attempt - 1))  # Експоненційний backoff: 15, 30, 60...
                        self.logger.info(f"Повторна спроба через {backoff_delay} секунд...")
                        await asyncio.sleep(backoff_delay)
                    continue
            self.logger.error(f"Не вдалося виконати запит до {endpoint} після {retries} спроб")
            return None

    async def _handle_response(self, response, endpoint):
        try:
            status = response.status
            text = await response.text()
            self.logger.debug(f"Відповідь від {endpoint}: статус={status}, текст={text}")
            if status == 200:
                data = await response.json()
                self.logger.debug(f"Успішний запит до {endpoint}: {data}")
                return data
            else:
                self.logger.error(f"Помилка API для {endpoint}: {status} - {text}")
                return None
        except Exception as e:
            self.logger.error(f"Помилка обробки відповіді для {endpoint}: {str(e)}")
            return None

    async def test_api_connection(self):
        """Перевірка підключення до API через /fapi/v1/time."""
        try:
            self.logger.info("Перевірка API через /fapi/v1/time...")
            async with self.session.get(f"{self.base_url}/fapi/v1/time", timeout=10) as response:
                status = response.status
                text = await response.text()
                self.logger.debug(f"Відповідь /fapi/v1/time: статус={status}, текст={text}")
                if status == 200:
                    data = await response.json()
                    self.logger.info(f"API підключення успішне: серверний час {data.get('serverTime')}")
                    return True
                else:
                    self.logger.error(f"Помилка перевірки /fapi/v1/time: {status} - {text}")
                    return False
        except Exception as e:
            self.logger.error(f"Помилка перевірки API: {str(e)}")
            return False

    async def get_balance(self):
        """Отримати баланс USDT або USDC з ф’ючерсного гаманця."""
        if not await self.test_api_connection():
            self.logger.error("Не вдалося підключитися до API. Перевірте ключі та мережу.")
            return 0.0

        data = await self._signed_request('GET', '/fapi/v2/balance')
        if not data:
            self.logger.error("Не вдалося отримати баланс: відповідь API пуста")
            return 0.0

        # Шукаємо USDT або USDC
        for asset in data:
            if asset['asset'] in ['USDT', 'USDC']:
                balance = float(asset['availableBalance'])
                self.logger.info(f"Доступний баланс {asset['asset']}: {balance}")
                return balance
        self.logger.warning("Не знайдено баланс USDT/USDC")
        return 0.0

    async def get_current_price(self, symbol):
        """Отримати поточну ціну символу."""
        try:
            async with self.session.get(f"{self.base_url}/fapi/v1/ticker/price", params={'symbol': symbol}) as response:
                status = response.status
                text = await response.text()
                self.logger.debug(f"Відповідь /fapi/v1/ticker/price для {symbol}: статус={status}, текст={text}")
                if status == 200:
                    data = await response.json()
                    price = float(data['price'])
                    self.logger.info(f"[{symbol}] Поточна ціна: ${price:.2f}")
                    return price
                self.logger.error(f"[{symbol}] Помилка отримання ціни: {status} - {text}")
                return None
        except Exception as e:
            self.logger.error(f"[{symbol}] Помилка отримання ціни: {str(e)}")
            return None

    async def get_symbol_precision(self, symbol):
        """Отримати точність quantity для символу."""
        try:
            async with self.session.get(f"{self.base_url}/fapi/v1/exchangeInfo") as response:
                status = response.status
                text = await response.text()
                self.logger.debug(f"Відповідь /fapi/v1/exchangeInfo для {symbol}: статус={status}, текст={text}")
                if status == 200:
                    data = await response.json()
                    for s in data['symbols']:
                        if s['symbol'] == symbol:
                            precision = s['quantityPrecision']
                            self.logger.info(f"[{symbol}] Точність quantity: {precision}")
                            return precision
            self.logger.warning(f"[{symbol}] Використовується точність за замовчуванням: 3")
            return 3
        except Exception as e:
            self.logger.error(f"[{symbol}] Помилка отримання precision: {str(e)}")
            return 3

    # НОВИЙ МЕТОД: Перевірити поточний margin type
    async def get_margin_type(self, symbol):
        """Отримати тип маржі для символу."""
        try:
            # ЗМІНА API PATH З V1 НА V2
            params = {'symbol': symbol}
            position_risk_data = await self._signed_request('GET', '/fapi/v2/positionRisk', params=params) 
            
            if not position_risk_data:
                return 'CROSS' # Припускаємо CROSS, якщо не отримали даних
            
            # Binance повертає список, навіть якщо запитується один символ
            position_data = [pos for pos in position_risk_data if pos['symbol'] == symbol]
            if position_data:
                return position_data[0]['marginType'].upper()
            return 'CROSS'
        except Exception as e:
            self.logger.error(f"[{symbol}] Помилка отримання margin type: {str(e)}")
            # [cite: 2, 7] (На основі логу: якщо v1 не працює, v2 більш імовірний)
            return 'CROSS'

    async def set_margin_type_isolated(self, symbol, check_only=False):
        """Встановити margin type на ISOLATED, з перевіркою поточного."""
        try:
            current_type = await self.get_margin_type(symbol)
            if current_type == 'ISOLATED':
                self.logger.info(f"[{symbol}] Margin type вже ISOLATED, пропускаємо.")
                self.initialized_symbols.add(symbol)
                return True

            if check_only:
                self.logger.info(f"[{symbol}] Margin type не ISOLATED, але check_only=True, повертаємо False")
                return False

            params = {
                'symbol': symbol,
                'marginType': 'ISOLATED'
            }
            data = await self._signed_request('POST', '/fapi/v1/marginType', params=params)
            if data:
                self.logger.info(f"[{symbol}] Успішно встановлено margin type ISOLATED")
                self.initialized_symbols.add(symbol)
                return True
            else:
                self.logger.error(f"[{symbol}] Не вдалося встановити режим маржі ISOLATED")
                return False
        except Exception as e:
            self.logger.error(f"[{symbol}] Помилка встановлення margin type: {str(e)}")
            return False

    async def set_leverage(self, symbol):
        """Встановити leverage для символу."""
        try:
            params = {
                'symbol': symbol,
                'leverage': self.leverage
            }
            data = await self._signed_request('POST', '/fapi/v1/leverage', params=params)
            if data:
                self.logger.info(f"[{symbol}] Успішно встановлено leverage {self.leverage}")
                self.initialized_symbols.add(symbol)
                return True
            else:
                self.logger.error(f"[{symbol}] Не вдалося встановити leverage")
                return False
        except Exception as e:
            self.logger.error(f"[{symbol}] Помилка встановлення leverage: {str(e)}")
            return False

    # ОНОВЛЕНИЙ МЕТОД: Ініціалізація для всіх символів з повторами та великими паузами
    async def setup_all_symbols(self, symbols):
        """Ініціалізувати margin type та leverage для списку символів з додатковими паузами."""
        self.logger.info(f"Ініціалізація {len(symbols)} символів...")
        successful = 0
        for i, symbol in enumerate(symbols):
            if symbol in self.initialized_symbols:
                self.logger.info(f"[{symbol}] Вже ініціалізовано, пропускаємо.")
                successful += 1
                continue
            if await self.set_margin_type_isolated(symbol):
                await asyncio.sleep(10)  # Пауза 10 сек після margin
                if await self.set_leverage(symbol):
                    successful += 1
                else:
                    self.logger.warning(f"[{symbol}] Leverage не встановлено, але margin OK.")
            else:
                self.logger.error(f"[{symbol}] Не вдалося встановити margin, пропускаємо symbol.")
            if i < len(symbols) - 1:  # Не пауза після останнього
                await asyncio.sleep(10)  # Збільшена затримка 10 сек між символами для testnet
        self.logger.info(f"Ініціалізація завершена: {successful}/{len(symbols)} успішно.")

    async def open_position(self, symbol, side, entry_price, atr_at_entry): # <--- ЗМІНА: sl_price замінено на atr_at_entry
        try:
            await asyncio.sleep(3)  # Затримка перед початком для rate limit

            balance = await self.get_balance()
            if balance <= 0:
                self.logger.warning(f"Недостатньо балансу: {balance}")
                return None

            # Перевірка, чи ініціалізовано символ
            if symbol not in self.initialized_symbols:
                self.logger.warning(f"[{symbol}] Символ не ініціалізовано заздалегідь, але продовжуємо (може бути CROSS).")

            # ЗМІНА 3: Розрахунок ризику (price_diff) на основі ATR при вході
            risk_amount = self.FIXED_RISK_AMOUNT
            price_diff = atr_at_entry * self.DEFAULT_STOP_MULTIPLIER
            
            if price_diff <= 0:
                self.logger.warning(f"[{symbol}] Недійсний price_diff (ATR * Multiplier): {price_diff}")
                return None

            # Розрахунок кількості: Quantity = (Risk_Amount / Price_Diff) / Entry_Price * Leverage
            # Ризик = Quantity * Price_Diff * Entry_Price (це не вірно для ф'ючерсів, де Margin = Quantity * Price / Leverage)
            # Правильно: Quantity = Risk_Amount / Price_Diff
            quantity = risk_amount / price_diff
            
            precision = await self.get_symbol_precision(symbol)
            quantity = round(quantity, precision)

            # Convert quantity to string without trailing zeros or .0
            quantity_str = f"{quantity:.{precision}f}".rstrip('0').rstrip('.') if '.' in f"{quantity:.{precision}f}" else f"{quantity:.{precision}f}"

            params = {
                'symbol': symbol,
                'side': 'BUY' if side == 'long' else 'SELL',
                'type': 'MARKET',
                'quantity': quantity_str
            }
            order_data = await self._signed_request('POST', '/fapi/v1/order', params=params)

            if not order_data:
                return None

            order_id = str(order_data['orderId'])

            position = {
                'order_id': order_id,
                'symbol': symbol,
                'side': side,
                'entry_time': datetime.now(),
                'entry_price': entry_price,
                'quantity': quantity,
                'active': True,
                'atr_at_entry': atr_at_entry, # <--- ЗБЕРІГАЄМО ATR для TPSLManager
                # 'sl_price': sl_price # <--- ВИЛУЧЕНО
            }
            self.positions[order_id] = position

            self.logger.info(f"[{symbol}] Відкрито {side.upper()} позицію ID: {order_id} | Quantity: {quantity} | Вхід: ${entry_price:.4f} | Ризик: ${risk_amount} | ATR_Stop_Dist: {price_diff:.4f}")

            await asyncio.sleep(3)  # Затримка після ордера
            return order_id
        except Exception as e:
            self.logger.error(f"[{symbol}] Помилка відкриття позиції: {str(e)}")
            return None

    # ОНОВЛЕНО: Тепер повертає словник з PNL та часом закриття
    async def close_position(self, order_id, exit_price, reason=""):
        if order_id not in self.positions or not self.positions[order_id]['active']:
            self.logger.warning(f"Позиція ID {order_id} не існує або не активна")
            return None

        position = self.positions[order_id]
        symbol = position['symbol']
        side = 'SELL' if position['side'] == 'long' else 'BUY'

        precision = await self.get_symbol_precision(symbol)
        quantity = position['quantity']
        quantity_str = f"{quantity:.{precision}f}".rstrip('0').rstrip('.') if '.' in f"{quantity:.{precision}f}" else f"{quantity:.{precision}f}"

        params = {
            'symbol': symbol,
            'side': side,
            'type': 'MARKET',
            'quantity': quantity_str,
            'reduceOnly': 'true'
        }
        close_data = await self._signed_request('POST', '/fapi/v1/order', params=params)

        if not close_data:
            return None

        position['active'] = False
        close_time = datetime.now().isoformat()

        pnl = (exit_price - position['entry_price']) * position['quantity'] if position['side'] == 'long' else (position['entry_price'] - exit_price) * position['quantity']
        commission_cost = (exit_price * position['quantity']) * self.commission * 2 # Комісія за відкриття та закриття
        net_pnl = pnl - commission_cost

        entry_value = position['entry_price'] * position['quantity']
        pnl_percent = (net_pnl / entry_value) * 100.0 * self.leverage if entry_value != 0 else 0.0

        trade_record = {
            'order_id': order_id, 'symbol': symbol, 'side': position['side'],
            'entry_price': position['entry_price'], 'exit_price': exit_price,
            'pnl': net_pnl, 'reason': reason
        }
        self.trade_history.append(trade_record)

        self.logger.info(f"[{symbol}] Закрито {position['side'].upper()} позицію ID: {order_id} | P&L: ${net_pnl:.4f} ({pnl_percent:.2f}%) | Причина: {reason}")

        if abs(pnl_percent) > 5:
            await self.send_trade_alert(trade_record)
        
        return {
            'pnl_percent': pnl_percent,
            'pnl_amount': net_pnl,
            'close_time': close_time,
        }

    async def send_trade_alert(self, trade_record):
        try:
            bot = Bot(token=TELEGRAM_TOKEN)
            emoji = "✅" if trade_record['pnl'] > 0 else "❌"
            message = (f"📈 Закрито значну угоду!\n"
                      f"{emoji} {trade_record['symbol']} {trade_record['side'].upper()}\n"
                      f"P&L: ${trade_record['pnl']:.2f}\n"
                      f"Причина: {trade_record['reason']}")
            await bot.send_message(chat_id=CHAT_ID, text=message)
        except Exception as e:
            self.logger.error(f"Помилка відправки алерту: {str(e)}")

    def get_open_positions(self) -> list:
        return [pos for pos in self.positions.values() if pos['active']]

    def get_stats(self) -> dict:
        if not self.trade_history:
            return {'win_rate': 0}
        total_trades = len(self.trade_history)
        winning_trades = sum(1 for trade in self.trade_history if trade['pnl'] > 0)
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
        return {'win_rate': win_rate}