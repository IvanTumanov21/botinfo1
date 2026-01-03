"""
Исполнение ордеров на Bybit
"""
from typing import Dict, Optional
from datetime import datetime, timezone
from loguru import logger

from src.config import RISK_MANAGEMENT
from src.database import (
    get_db, Signal, SignalStatus, Position, PositionStatus, Trade
)
from src.exchange import BybitExchange


class OrderExecutor:
    """Исполнитель ордеров"""
    
    def __init__(self, exchange: BybitExchange):
        self.exchange = exchange
        
    async def execute_buy_from_signal(self, signal_id: int, amount_usdt_override: Optional[float] = None) -> Optional[Position]:
        """
        Исполняет покупку по сигналу.
        Создаёт лимитный ордер и позицию в БД.
        amount_usdt_override — если передан, берём фиксированную сумму в USDT.
        """
        with get_db() as db:
            signal = db.query(Signal).filter(Signal.id == signal_id).first()
            
            if not signal or signal.status != SignalStatus.ACCEPTED:
                logger.warning(f"Сигнал {signal_id} не найден или не принят")
                return None
            
            symbol = signal.symbol
            entry_price = signal.entry_price
            stop_loss = signal.stop_loss
            tp1 = signal.tp1
            tp2 = signal.tp2
        
        try:
            # Получаем баланс
            balance = await self.exchange.get_balance()
            usdt_free = balance.get('USDT', {}).get('free', 0)
            
            if usdt_free < 10:  # Минимум $10
                logger.error(f"Недостаточно USDT: {usdt_free:.2f}")
                return None
            
            # Рассчитываем размер позиции
            if amount_usdt_override and amount_usdt_override > 0:
                position_size_usdt = min(amount_usdt_override, usdt_free)
            else:
                position_size_usdt = usdt_free * RISK_MANAGEMENT["position_size_pct"]
            
            # Проверяем риск на сделку
            risk_pct = (entry_price - stop_loss) / entry_price
            if risk_pct > RISK_MANAGEMENT["max_risk_per_trade"]:
                # Уменьшаем размер чтобы риск был <= 1%
                max_loss = usdt_free * RISK_MANAGEMENT["max_risk_per_trade"]
                position_size_usdt = max_loss / risk_pct
            
            # Количество монет
            amount = position_size_usdt / entry_price
            
            # Создаём лимитный ордер
            order = await self.exchange.create_limit_order(
                symbol=symbol,
                side="BUY",
                amount=amount,
                price=entry_price
            )
            
            if not order:
                logger.error(f"Не удалось создать ордер на {symbol}")
                return None
            
            # Сохраняем позицию в БД и возвращаем данные как словарь (не ORM-объект)
            with get_db() as db:
                position = Position(
                    signal_id=signal_id,
                    symbol=symbol,
                    side="BUY",
                    entry_price=entry_price,
                    entry_amount=amount,
                    entry_value_usdt=position_size_usdt,
                    current_amount=amount,
                    stop_loss=stop_loss,
                    tp1=tp1,
                    tp2=tp2,
                    status=PositionStatus.OPEN,
                )
                db.add(position)
                db.flush()
                position_id = position.id
                
                # Записываем сделку
                trade = Trade(
                    position_id=position_id,
                    symbol=symbol,
                    side="BUY",
                    order_type="LIMIT",
                    price=entry_price,
                    amount=amount,
                    value_usdt=position_size_usdt,
                    order_id=order.get('id'),
                    reason="SIGNAL",
                )
                db.add(trade)
                
                # Обновляем сигнал
                signal = db.query(Signal).filter(Signal.id == signal_id).first()
                if signal:
                    signal.status = SignalStatus.EXECUTED
                
                db.commit()
                
                position_data = {
                    "id": position_id,
                    "symbol": symbol,
                    "entry_price": entry_price,
                    "entry_amount": amount,
                    "entry_value_usdt": position_size_usdt,
                }
            
            logger.info(f"✅ Ордер на покупку {symbol}: {amount:.6f} @ {entry_price:.6f}")
            return position_data
            
        except Exception as e:
            logger.error(f"Ошибка исполнения покупки: {e}")
            return None
    
    async def execute_sell(
        self, 
        position_id: int, 
        amount: float,
        reason: str = "MANUAL",
        use_market: bool = True
    ) -> Optional[Trade]:
        """
        Исполняет продажу части или всей позиции.
        
        reason: MANUAL, TP1, TP2, TP3, SL, TRAILING
        """
        with get_db() as db:
            position = db.query(Position).filter(Position.id == position_id).first()
            
            if not position:
                logger.error(f"Позиция {position_id} не найдена")
                return None
            
            if position.status in [
                PositionStatus.CLOSED_TP, 
                PositionStatus.CLOSED_SL, 
                PositionStatus.CLOSED_MANUAL
            ]:
                logger.warning(f"Позиция {position_id} уже закрыта")
                return None
            
            symbol = position.symbol
            entry_price = position.entry_price
            
            # Ограничиваем amount доступным количеством
            sell_amount = min(amount, position.current_amount)
        
        try:
            # Получаем текущую цену
            ticker = await self.exchange.get_ticker(symbol)
            current_price = ticker['last'] if ticker else entry_price
            
            # Создаём ордер
            if use_market:
                order = await self.exchange.create_market_order(
                    symbol=symbol,
                    side="SELL",
                    amount=sell_amount
                )
            else:
                order = await self.exchange.create_limit_order(
                    symbol=symbol,
                    side="SELL",
                    amount=sell_amount,
                    price=current_price
                )
            
            if not order:
                logger.error(f"Не удалось создать ордер на продажу {symbol}")
                return None
            
            # Рассчитываем P&L
            sell_value = sell_amount * current_price
            entry_value = sell_amount * entry_price
            pnl_usdt = sell_value - entry_value
            pnl_pct = (current_price / entry_price - 1) * 100
            
            # Обновляем БД
            with get_db() as db:
                position = db.query(Position).filter(Position.id == position_id).first()
                
                # Записываем сделку
                trade = Trade(
                    position_id=position_id,
                    symbol=symbol,
                    side="SELL",
                    order_type="MARKET" if use_market else "LIMIT",
                    price=current_price,
                    amount=sell_amount,
                    value_usdt=sell_value,
                    order_id=order.get('id'),
                    pnl_usdt=pnl_usdt,
                    pnl_pct=pnl_pct,
                    reason=reason,
                )
                db.add(trade)
                
                # Обновляем позицию
                position.current_amount -= sell_amount
                position.realized_pnl += pnl_usdt
                
                # Обновляем статус
                if position.current_amount <= 0.0001:
                    # Позиция полностью закрыта
                    position.status = self._get_close_status(reason)
                    position.closed_at = datetime.now(timezone.utc)
                    position.close_price = current_price
                    position.close_reason = reason
                    position.total_pnl_usdt = position.realized_pnl
                    position.total_pnl_pct = (
                        position.realized_pnl / position.entry_value_usdt * 100
                    )
                elif reason == "TP1":
                    position.status = PositionStatus.PARTIAL_TP1
                elif reason == "TP2":
                    position.status = PositionStatus.PARTIAL_TP2
                
                db.commit()
                
                # Формируем данные для возврата
                trade_data = {
                    'side': 'SELL',
                    'symbol': symbol,
                    'price': current_price,
                    'amount': sell_amount,
                    'value_usdt': sell_value,
                    'reason': reason,
                    'pnl_usdt': pnl_usdt,
                    'pnl_pct': pnl_pct,
                }
                
                # Обновляем дневную статистику если закрыта
                if position.status in [
                    PositionStatus.CLOSED_TP, 
                    PositionStatus.CLOSED_SL,
                    PositionStatus.CLOSED_MANUAL
                ]:
                    from src.database import DailyStats
                    today = datetime.now(timezone.utc).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    stats = db.query(DailyStats).filter(
                        DailyStats.date == today
                    ).first()
                    
                    if stats:
                        stats.total_pnl_usdt += position.total_pnl_usdt
                        if position.total_pnl_usdt >= 0:
                            stats.trades_won += 1
                        else:
                            stats.trades_lost += 1
                            if position.status == PositionStatus.CLOSED_SL:
                                stats.stop_losses_today += 1
                
                db.flush()
                trade_result = {
                    'symbol': symbol,
                    'side': 'SELL',
                    'price': current_price,
                    'amount': sell_amount,
                    'value_usdt': sell_value,
                    'pnl_usdt': pnl_usdt,
                    'pnl_pct': pnl_pct,
                    'reason': reason,
                }
            
            logger.info(
                f"✅ Продажа {symbol}: {sell_amount:.6f} @ {current_price:.6f} | "
                f"P&L: {pnl_pct:+.2f}% (${pnl_usdt:+.2f})"
            )
            
            return trade_result
            
        except Exception as e:
            logger.error(f"Ошибка исполнения продажи: {e}")
            return None
    
    def _get_close_status(self, reason: str) -> PositionStatus:
        """Определяет статус закрытия по причине"""
        if reason in ["TP1", "TP2", "TP3", "TRAILING"]:
            return PositionStatus.CLOSED_TP
        elif reason == "SL":
            return PositionStatus.CLOSED_SL
        else:
            return PositionStatus.CLOSED_MANUAL
