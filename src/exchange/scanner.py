"""
Сканер рынка - поиск сигналов на покупку
"""
import asyncio
from typing import Dict, List, Optional
from datetime import datetime, timezone, timedelta
from loguru import logger

from src.config import (
    TIMEFRAMES, SIGNAL_CONDITIONS, ANTI_FOMO, 
    SCAN_INTERVALS, RISK_MANAGEMENT
)
from src.database import get_db, Signal, SignalCooldown, DailyStats, Position, PositionStatus
from src.exchange.exchange import BybitExchange
from src.exchange.indicators import (
    calculate_indicators, detect_accumulation, 
    detect_breakout, check_false_pump_filter, calculate_levels
)


class MarketScanner:
    """Сканер рынка для поиска breakout сигналов"""
    
    def __init__(self, exchange: BybitExchange):
        self.exchange = exchange
        self.symbols: List[str] = []
        self.last_universe_update: Optional[datetime] = None
        
    async def update_universe(self):
        """Обновляет список торгуемых пар"""
        self.symbols = await self.exchange.get_tradeable_symbols(force_refresh=True)
        self.last_universe_update = datetime.now(timezone.utc)
        logger.info(f"📊 Обновлён список: {len(self.symbols)} пар")
        
    def should_update_universe(self) -> bool:
        """Проверяет, нужно ли обновить список пар"""
        if not self.last_universe_update:
            return True
        age = (datetime.now(timezone.utc) - self.last_universe_update).total_seconds()
        return age >= SCAN_INTERVALS["universe_update_sec"]
    
    async def check_market_conditions(self) -> Dict:
        """
        Проверяет глобальные условия рынка.
        Возвращает словарь с причинами, почему нельзя торговать.
        """
        logger.debug("🔍 Проверка условий рынка...")
        conditions = {
            "can_trade": True,
            "reasons": []
        }
        
        # 1. Проверка падения BTC
        try:
            btc_change = await self.exchange.get_btc_change_1h()
            logger.debug(f"BTC 1h change: {btc_change*100:.2f}%")
            if btc_change < ANTI_FOMO["btc_drop_threshold"]:
                conditions["can_trade"] = False
                conditions["reasons"].append(
                    f"BTC падает ({btc_change*100:.2f}% за 1ч)"
                )
        except Exception as e:
            logger.error(f"Ошибка проверки BTC: {e}")
        
        # 2. Проверка ночных часов UTC
        now_utc = datetime.now(timezone.utc)
        hour = now_utc.hour
        night_start, night_end = ANTI_FOMO["night_hours_utc"]
        if night_start <= hour < night_end:
            conditions["can_trade"] = False
            conditions["reasons"].append(
                f"Ночное время UTC ({hour}:00)"
            )
        
        # 3. Проверка дневных стопов
        with get_db() as db:
            today = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
            stats = db.query(DailyStats).filter(
                DailyStats.date == today
            ).first()
            
            if stats and stats.stop_losses_today >= RISK_MANAGEMENT["max_daily_losses"]:
                conditions["can_trade"] = False
                conditions["reasons"].append(
                    f"Достигнут лимит стопов ({stats.stop_losses_today})"
                )
        
        # 4. Проверка количества открытых позиций
        with get_db() as db:
            open_positions = db.query(Position).filter(
                Position.status.in_([
                    PositionStatus.OPEN,
                    PositionStatus.PARTIAL_TP1,
                    PositionStatus.PARTIAL_TP2
                ])
            ).count()
            
            if open_positions >= RISK_MANAGEMENT["max_positions"]:
                conditions["can_trade"] = False
                conditions["reasons"].append(
                    f"Макс позиций ({open_positions}/{RISK_MANAGEMENT['max_positions']})"
                )
        
        return conditions
    
    async def check_symbol_cooldown(self, symbol: str) -> bool:
        """
        Проверяет, можно ли отправить сигнал по паре.
        Возвращает True если можно (кулдаун прошёл).
        """
        with get_db() as db:
            cooldown = db.query(SignalCooldown).filter(
                SignalCooldown.symbol == symbol
            ).first()
            
            if not cooldown:
                return True
            
            hours_since = (
                datetime.now(timezone.utc) - cooldown.last_signal_at
            ).total_seconds() / 3600
            
            return hours_since >= ANTI_FOMO["signal_cooldown_hours"]
    
    async def analyze_symbol(self, symbol: str) -> Optional[Dict]:
        """
        Полный анализ одной пары.
        Возвращает словарь с сигналом или None.
        """
        try:
            # 1. Проверяем кулдаун
            if not await self.check_symbol_cooldown(symbol):
                return None
            
            # 2. Получаем свечи (основной ТФ - 5m)
            ohlcv = await self.exchange.get_ohlcv(
                symbol, 
                TIMEFRAMES["main"],
                limit=150
            )
            
            if not ohlcv or len(ohlcv) < 120:
                return None
            
            # 3. Рассчитываем индикаторы
            df = calculate_indicators(ohlcv)
            if df is None:
                return None
            
            # 4. Проверяем фазу накопления
            is_accumulation, acc_metrics = detect_accumulation(df)
            if not is_accumulation:
                return None  # Нет накопления - пропускаем
            
            # 5. Проверяем breakout
            is_breakout, br_metrics = detect_breakout(df)
            if not is_breakout:
                return None  # Нет пробоя - пропускаем
            
            # 6. Получаем данные для фильтров
            ticker = await self.exchange.get_ticker(symbol)
            orderbook = await self.exchange.get_orderbook(symbol, limit=10)
            
            if not ticker or not orderbook:
                return None
            
            # Спред
            bid = orderbook['bids'][0][0] if orderbook['bids'] else 0
            ask = orderbook['asks'][0][0] if orderbook['asks'] else 0
            spread = (ask - bid) / bid if bid > 0 else 1
            
            # Bid/Ask ratio (сумма объёмов)
            bid_volume = sum(b[1] for b in orderbook['bids'][:5])
            ask_volume = sum(a[1] for a in orderbook['asks'][:5])
            bid_ask_ratio = bid_volume / ask_volume if ask_volume > 0 else 0
            
            # 7. Фильтр ложных пампов
            passed_filter, filter_metrics = check_false_pump_filter(
                df, spread, bid_ask_ratio
            )
            
            if not passed_filter:
                return None  # Не прошёл фильтр
            
            # 8. Проверка FOMO (не покупать если +10% от лоя дня)
            # Получаем дневной low
            ohlcv_1d = await self.exchange.get_ohlcv(symbol, "1440", limit=1)
            if ohlcv_1d:
                daily_low = ohlcv_1d[-1][3]  # low
                current_price = df.iloc[-1]['close']
                from_low_pct = (current_price - daily_low) / daily_low
                
                if from_low_pct > ANTI_FOMO["max_from_daily_low_pct"]:
                    logger.debug(f"{symbol}: +{from_low_pct*100:.1f}% от лоя дня - пропуск")
                    return None
            
            # 9. Рассчитываем уровни
            last = df.iloc[-1]
            levels = calculate_levels(
                entry_price=last['close'],
                atr=last['atr14'],
                ema28=last['ema28'],
                low_20=last['low_20']
            )
            
            # 10. Формируем сигнал
            signal = {
                "symbol": symbol,
                "price": last['close'],
                "candle_growth_pct": filter_metrics["candle_growth_pct"],
                "volume_ratio": br_metrics["volume_ratio"],
                "spread_pct": filter_metrics["spread_pct"],
                "rsi": filter_metrics["rsi"],
                "ema7": last['ema7'],
                "ema14": last['ema14'],
                "ema28": last['ema28'],
                "ema100": last['ema100'],
                "accumulation_detected": True,
                "accumulation_range": acc_metrics["range_ratio"],
                "levels": levels,
                "metrics": {
                    "accumulation": acc_metrics,
                    "breakout": br_metrics,
                    "filter": filter_metrics,
                }
            }
            
            return signal
            
        except Exception as e:
            logger.error(f"Ошибка анализа {symbol}: {e}")
            return None
    
    async def scan_all(self) -> List[Dict]:
        """
        Сканирует все пары и возвращает список сигналов.
        """
        # Обновляем список если нужно
        if self.should_update_universe():
            await self.update_universe()
        
        logger.info("🔍 Проверка рыночных условий...")
        
        # Проверяем глобальные условия
        market = await self.check_market_conditions()
        if not market["can_trade"]:
            logger.info(f"⏸ Торговля приостановлена: {', '.join(market['reasons'])}")
            return []
        
        logger.info("✅ Условия ОК, начинаем сканирование...")
        
        if not self.symbols:
            logger.warning("Нет пар для сканирования")
            return []
        
        signals = []
        total = len(self.symbols)
        
        logger.info(f"🔍 Начинаем сканирование {total} пар...")
        
        # Сканируем с задержкой для rate limit
        for i, symbol in enumerate(self.symbols):
            try:
                signal = await self.analyze_symbol(symbol)
                if signal:
                    signals.append(signal)
                    logger.info(f"🚀 Найден сигнал: {symbol}")
                
                # Rate limit: пауза каждые 10 пар + логирование прогресса
                if (i + 1) % 10 == 0:
                    logger.info(f"📊 Просканировано {i+1}/{total} пар...")
                    await asyncio.sleep(1)
                    
            except Exception as e:
                logger.error(f"Ошибка сканирования {symbol}: {e}")
                continue
        
        logger.info(f"✅ Сканирование завершено. Сигналов: {len(signals)}")
        return signals
    
    async def save_signal_to_db(self, signal: Dict) -> int:
        """Сохраняет сигнал в БД и возвращает ID"""
        with get_db() as db:
            db_signal = Signal(
                symbol=signal["symbol"],
                price=signal["price"],
                candle_growth_pct=signal["candle_growth_pct"],
                volume_ratio=signal["volume_ratio"],
                spread_pct=signal["spread_pct"],
                rsi=signal["rsi"],
                ema7=signal["ema7"],
                ema14=signal["ema14"],
                ema28=signal["ema28"],
                ema100=signal["ema100"],
                entry_price=signal["levels"]["entry_high"],
                stop_loss=signal["levels"]["stop_loss"],
                tp1=signal["levels"]["tp1"],
                tp2=signal["levels"]["tp2"],
                tp3=signal["levels"]["tp3"],
                accumulation_detected=signal["accumulation_detected"],
                accumulation_range=signal["accumulation_range"],
            )
            db.add(db_signal)
            db.flush()
            signal_id = db_signal.id
            
            # Обновляем кулдаун
            cooldown = db.query(SignalCooldown).filter(
                SignalCooldown.symbol == signal["symbol"]
            ).first()
            
            if cooldown:
                cooldown.last_signal_at = datetime.now(timezone.utc)
            else:
                cooldown = SignalCooldown(
                    symbol=signal["symbol"],
                    last_signal_at=datetime.now(timezone.utc)
                )
                db.add(cooldown)
            
            # Обновляем дневную статистику
            today = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            stats = db.query(DailyStats).filter(
                DailyStats.date == today
            ).first()
            
            if stats:
                stats.signals_sent += 1
            else:
                stats = DailyStats(date=today, signals_sent=1)
                db.add(stats)
            
            return signal_id
