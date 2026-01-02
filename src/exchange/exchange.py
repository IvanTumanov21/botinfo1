"""
Подключение к Bybit через pybit + ccxt
"""
import asyncio
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
from loguru import logger
from pybit.unified_trading import HTTP
import ccxt.async_support as ccxt

from src.config import (
    BYBIT_API_KEY, BYBIT_SECRET, BYBIT_TESTNET,
    ASSET_FILTERS, TIMEFRAMES
)


class BybitExchange:
    """Класс для работы с биржей Bybit"""
    
    def __init__(self):
        self.api_key = BYBIT_API_KEY
        self.secret = BYBIT_SECRET
        self.testnet = BYBIT_TESTNET
        
        # pybit для REST API
        self.client: Optional[HTTP] = None
        
        # ccxt для свечей и универсальных методов
        self.ccxt: Optional[ccxt.bybit] = None
        
        # Кэш
        self._symbols_cache: List[str] = []
        self._cache_time: Optional[datetime] = None
        
    async def connect(self):
        """Инициализация подключений"""
        try:
            # pybit
            self.client = HTTP(
                api_key=self.api_key,
                api_secret=self.secret,
                testnet=self.testnet,
            )
            
            # ccxt
            self.ccxt = ccxt.bybit({
                'apiKey': self.api_key,
                'secret': self.secret,
                'enableRateLimit': True,
                'options': {
                    'defaultType': 'spot',
                }
            })
            
            if self.testnet:
                self.ccxt.set_sandbox_mode(True)
                
            await self.ccxt.load_markets()
            
            logger.info(f"✅ Подключение к Bybit {'TESTNET' if self.testnet else 'MAINNET'}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к Bybit: {e}")
            return False
    
    async def close(self):
        """Закрытие соединений"""
        if self.ccxt:
            await self.ccxt.close()
            
    async def get_tradeable_symbols(self, force_refresh: bool = False) -> List[str]:
        """
        Получает список торгуемых пар по фильтрам:
        - Цена 0.0005 - 1.0 USDT
        - Оборот ≥ 200k USDT
        - Исключает BTC, ETH, stables
        """
        # Проверяем кэш (5 минут)
        if not force_refresh and self._symbols_cache and self._cache_time:
            age = (datetime.now(timezone.utc) - self._cache_time).total_seconds()
            if age < 300:
                return self._symbols_cache
        
        try:
            # Получаем все SPOT тикеры
            tickers = await self.ccxt.fetch_tickers()
            
            valid_symbols = []
            
            for symbol, ticker in tickers.items():
                # Только USDT пары
                if not symbol.endswith('/USDT'):
                    continue
                
                base = symbol.split('/')[0]
                
                # Исключаем
                if base in ASSET_FILTERS["excluded_bases"]:
                    continue
                
                # Проверяем цену
                price = ticker.get('last') or ticker.get('close') or 0
                if not (ASSET_FILTERS["min_price"] <= price <= ASSET_FILTERS["max_price"]):
                    continue
                
                # Проверяем объём
                quote_volume = ticker.get('quoteVolume') or 0
                if quote_volume < ASSET_FILTERS["min_volume_24h"]:
                    continue
                
                valid_symbols.append(symbol)
            
            # Обновляем кэш
            self._symbols_cache = valid_symbols
            self._cache_time = datetime.now(timezone.utc)
            
            logger.info(f"📊 Найдено {len(valid_symbols)} подходящих пар")
            return valid_symbols
            
        except Exception as e:
            logger.error(f"Ошибка получения символов: {e}")
            return self._symbols_cache or []
    
    async def get_ohlcv(
        self, 
        symbol: str, 
        timeframe: str = "5m",
        limit: int = 100
    ) -> List[List]:
        """
        Получает свечи (OHLCV)
        Формат: [[timestamp, open, high, low, close, volume], ...]
        """
        try:
            # Маппинг таймфреймов
            tf_map = {"1": "1m", "5": "5m", "15": "15m", "60": "1h"}
            tf = tf_map.get(timeframe, timeframe)
            
            ohlcv = await self.ccxt.fetch_ohlcv(symbol, tf, limit=limit)
            return ohlcv
            
        except Exception as e:
            logger.error(f"Ошибка получения свечей {symbol}: {e}")
            return []
    
    async def get_ticker(self, symbol: str) -> Optional[Dict]:
        """Получает текущий тикер"""
        try:
            ticker = await self.ccxt.fetch_ticker(symbol)
            return ticker
        except Exception as e:
            logger.error(f"Ошибка тикера {symbol}: {e}")
            return None
    
    async def get_orderbook(self, symbol: str, limit: int = 5) -> Optional[Dict]:
        """Получает стакан заявок"""
        try:
            orderbook = await self.ccxt.fetch_order_book(symbol, limit)
            return orderbook
        except Exception as e:
            logger.error(f"Ошибка стакана {symbol}: {e}")
            return None
    
    async def get_balance(self) -> Dict[str, float]:
        """Получает баланс аккаунта"""
        try:
            balance = await self.ccxt.fetch_balance()
            result = {}
            for currency, data in balance.items():
                if isinstance(data, dict) and data.get('free', 0) > 0:
                    result[currency] = {
                        'free': data.get('free', 0),
                        'used': data.get('used', 0),
                        'total': data.get('total', 0),
                    }
            return result
        except Exception as e:
            logger.error(f"Ошибка получения баланса: {e}")
            return {}
    
    async def create_limit_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float
    ) -> Optional[Dict]:
        """Создаёт лимитный ордер"""
        try:
            order = await self.ccxt.create_limit_order(
                symbol=symbol,
                side=side.lower(),
                amount=amount,
                price=price
            )
            logger.info(f"✅ Ордер создан: {side} {amount} {symbol} @ {price}")
            return order
        except Exception as e:
            logger.error(f"❌ Ошибка создания ордера: {e}")
            return None
    
    async def create_market_order(
        self,
        symbol: str,
        side: str,
        amount: float
    ) -> Optional[Dict]:
        """Создаёт маркет ордер"""
        try:
            order = await self.ccxt.create_market_order(
                symbol=symbol,
                side=side.lower(),
                amount=amount
            )
            logger.info(f"✅ Маркет ордер: {side} {amount} {symbol}")
            return order
        except Exception as e:
            logger.error(f"❌ Ошибка маркет ордера: {e}")
            return None
    
    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Отменяет ордер"""
        try:
            await self.ccxt.cancel_order(order_id, symbol)
            logger.info(f"Ордер {order_id} отменён")
            return True
        except Exception as e:
            logger.error(f"Ошибка отмены ордера: {e}")
            return False
    
    async def get_btc_change_1h(self) -> float:
        """Получает изменение BTC за 1 час (для фильтра)"""
        try:
            ohlcv = await self.get_ohlcv("BTC/USDT", "60", limit=2)
            if len(ohlcv) >= 2:
                prev_close = ohlcv[-2][4]
                curr_close = ohlcv[-1][4]
                change = (curr_close - prev_close) / prev_close
                return change
            return 0.0
        except Exception as e:
            logger.error(f"Ошибка BTC change: {e}")
            return 0.0
