"""
Менеджер позиций - проверка SL/TP, trailing stop
"""
import asyncio
from typing import List, Optional
from datetime import datetime, timezone
from loguru import logger

from src.config import RISK_MANAGEMENT
from src.database import get_db, Position, PositionStatus, DailyStats
from src.exchange import BybitExchange
from src.trading.executor import OrderExecutor


class PositionManager:
    """Управление открытыми позициями"""
    
    def __init__(self, exchange: BybitExchange, executor: OrderExecutor):
        self.exchange = exchange
        self.executor = executor
        
    async def check_all_positions(self) -> List[dict]:
        """
        Проверяет все открытые позиции на SL/TP.
        Возвращает список выполненных действий.
        """
        actions = []
        
        with get_db() as db:
            positions = db.query(Position).filter(
                Position.status.in_([
                    PositionStatus.OPEN,
                    PositionStatus.PARTIAL_TP1,
                    PositionStatus.PARTIAL_TP2
                ])
            ).all()
            
            # Копируем данные чтобы не держать сессию открытой
            positions_data = [
                {
                    'id': p.id,
                    'symbol': p.symbol,
                    'entry_price': p.entry_price,
                    'current_amount': p.current_amount,
                    'stop_loss': p.stop_loss,
                    'tp1': p.tp1,
                    'tp2': p.tp2,
                    'max_price': p.max_price,
                    'trailing_stop': p.trailing_stop,
                    'status': p.status,
                }
                for p in positions
            ]
        
        for pos in positions_data:
            action = await self._check_position(pos)
            if action:
                actions.append(action)
        
        return actions
    
    async def _check_position(self, pos: dict) -> Optional[dict]:
        """Проверяет одну позицию"""
        try:
            # Проверяем, есть ли актив на балансе (синхронизация с биржей)
            balance = await self.exchange.get_balance()
            currency = pos['symbol'].split('/')[0]  # IO/USDT -> IO
            
            # Если актива вообще нет в балансе (API не вернул его), считаем что его 0
            amount_on_exchange = 0
            if balance and currency in balance:
                amount_on_exchange = float(balance[currency].get('total', 0) or 0)
            
            # Если на бирже осталось меньше 5% от позиции (учитываем dust ~0.06), считаем закрытой
            # Минимальный порог - 0.1 токена (dust)
            dust_threshold = max(0.1, pos['current_amount'] * 0.05)
            
            if amount_on_exchange < dust_threshold:
                logger.warning(f"⚠️ Позиция {pos['symbol']} закрыта вручную на бирже (остаток {amount_on_exchange:.4f} < {dust_threshold:.4f}), синхронизируем БД")
                
                with get_db() as db:
                    position = db.query(Position).filter(Position.id == pos['id']).first()
                    if position:
                        # Создаём Trade запись для истории
                        from src.database.models import Trade
                        
                        # Пытаемся получить текущую цену для расчёта PnL
                        try:
                            ticker = await self.exchange.get_ticker(pos['symbol'])
                            current_price = ticker['last'] if ticker else pos['entry_price']
                        except:
                            current_price = pos['entry_price']
                        
                        # Рассчитываем PnL
                        pnl_usdt = (current_price - pos['entry_price']) * pos['current_amount']
                        pnl_pct = (current_price / pos['entry_price'] - 1) * 100
                        
                        trade = Trade(
                            position_id=pos['id'],
                            symbol=pos['symbol'],
                            side="SELL",
                            price=current_price,
                            amount=pos['current_amount'],
                            value_usdt=pos['current_amount'] * current_price,
                            reason="MANUAL_EXTERNAL",
                            pnl_usdt=pnl_usdt,
                            pnl_pct=pnl_pct,
                        )
                        db.add(trade)
                        
                        position.status = PositionStatus.CLOSED_MANUAL
                        position.closed_at = datetime.now(timezone.utc)
                        position.close_reason = "MANUAL_EXTERNAL"
                        position.close_price = current_price
                        position.total_pnl_usdt = pnl_usdt
                        
                        db.commit()
                
                return {
                    'action': 'SYNC_CLOSED',
                    'position_id': pos['id'],
                    'symbol': pos['symbol'],
                    'side': 'SELL',
                    'reason': 'Закрыто вручную на бирже',
                    'price': current_price,
                    'amount': pos['current_amount'],
                    'pnl_pct': pnl_pct,
                    'pnl_usdt': pnl_usdt,
                }
            
            # Получаем текущую цену
            ticker = await self.exchange.get_ticker(pos['symbol'])
            if not ticker:
                return None
            
            current_price = ticker['last']
            entry_price = pos['entry_price']
            
            # Обновляем max_price и trailing_stop
            await self._update_trailing(pos['id'], current_price, entry_price)
            
            # Проверяем Stop Loss
            if current_price <= pos['stop_loss']:
                logger.warning(f"🔴 SL сработал для {pos['symbol']}")
                
                result = await self.executor.execute_sell(
                    position_id=pos['id'],
                    amount=pos['current_amount'],
                    reason="SL",
                    use_market=True
                )
                
                if result:
                    return {
                        'action': 'SL',
                        'position_id': pos['id'],
                        'symbol': result['symbol'],
                        'side': result['side'],
                        'price': result['price'],
                        'amount': result['amount'],
                        'value_usdt': result['value_usdt'],
                        'reason': result['reason'],
                        'pnl_pct': result['pnl_pct'],
                        'pnl_usdt': result['pnl_usdt'],
                    }
            
            # Проверяем TP1 (если ещё не сработал)
            if pos['status'] == PositionStatus.OPEN and current_price >= pos['tp1']:
                logger.info(f"🎯 TP1 для {pos['symbol']}")
                
                # Продаём 30%
                sell_amount = pos['current_amount'] * RISK_MANAGEMENT['tp1_close_pct']
                
                result = await self.executor.execute_sell(
                    position_id=pos['id'],
                    amount=sell_amount,
                    reason="TP1",
                    use_market=True
                )
                
                if result:
                    return {
                        'action': 'TP1',
                        'position_id': pos['id'],
                        'symbol': result['symbol'],
                        'side': result['side'],
                        'price': result['price'],
                        'amount': result['amount'],
                        'value_usdt': result['value_usdt'],
                        'reason': result['reason'],
                        'pnl_pct': result['pnl_pct'],
                        'pnl_usdt': result['pnl_usdt'],
                    }
            
            # Проверяем TP2 (если TP1 уже сработал)
            if pos['status'] == PositionStatus.PARTIAL_TP1 and current_price >= pos['tp2']:
                logger.info(f"🎯 TP2 для {pos['symbol']}")
                
                # Продаём ещё 30% (от оставшегося ~43%)
                sell_amount = pos['current_amount'] * (RISK_MANAGEMENT['tp2_close_pct'] / 0.7)
                
                result = await self.executor.execute_sell(
                    position_id=pos['id'],
                    amount=sell_amount,
                    reason="TP2",
                    use_market=True
                )
                
                if result:
                    return {
                        'action': 'TP2',
                        'position_id': pos['id'],
                        'symbol': result['symbol'],
                        'side': result['side'],
                        'price': result['price'],
                        'amount': result['amount'],
                        'value_usdt': result['value_usdt'],
                        'reason': result['reason'],
                        'pnl_pct': result['pnl_pct'],
                        'pnl_usdt': result['pnl_usdt'],
                    }
            
            # Проверяем Trailing Stop (после TP2)
            if pos['status'] == PositionStatus.PARTIAL_TP2:
                trailing = pos.get('trailing_stop', 0)
                if trailing > 0 and current_price <= trailing:
                    logger.info(f"📉 Trailing Stop для {pos['symbol']}")
                    
                    result = await self.executor.execute_sell(
                        position_id=pos['id'],
                        amount=pos['current_amount'],
                        reason="TRAILING",
                        use_market=True
                    )
                    
                    if result:
                        return {
                            'action': 'TRAILING',
                            'position_id': pos['id'],
                            'symbol': result['symbol'],
                            'side': result['side'],
                            'price': result['price'],
                            'amount': result['amount'],
                            'value_usdt': result['value_usdt'],
                            'reason': result['reason'],
                            'pnl_pct': result['pnl_pct'],
                            'pnl_usdt': result['pnl_usdt'],
                        }
            
            return None
            
        except Exception as e:
            logger.error(f"Ошибка проверки позиции {pos['symbol']}: {e}")
            return None
    
    async def _update_trailing(
        self, 
        position_id: int, 
        current_price: float,
        entry_price: float
    ):
        """Обновляет trailing stop"""
        with get_db() as db:
            position = db.query(Position).filter(Position.id == position_id).first()
            if not position:
                return
            
            # Обновляем max_price
            if position.max_price is None or current_price > position.max_price:
                position.max_price = current_price
            
            # Trailing stop активируется после TP2
            if position.status == PositionStatus.PARTIAL_TP2:
                # Trailing = 3% ниже максимума
                new_trailing = position.max_price * 0.97
                
                # Обновляем только если новый trailing выше
                if position.trailing_stop is None or new_trailing > position.trailing_stop:
                    position.trailing_stop = new_trailing
    
    async def close_all_positions(self, reason: str = "MANUAL") -> int:
        """Закрывает все открытые позиции"""
        closed = 0
        
        with get_db() as db:
            positions = db.query(Position).filter(
                Position.status.in_([
                    PositionStatus.OPEN,
                    PositionStatus.PARTIAL_TP1,
                    PositionStatus.PARTIAL_TP2
                ])
            ).all()
            
            position_ids = [p.id for p in positions]
        
        for pos_id in position_ids:
            with get_db() as db:
                pos = db.query(Position).filter(Position.id == pos_id).first()
                if pos:
                    result = await self.executor.execute_sell(
                        position_id=pos_id,
                        amount=pos.current_amount,
                        reason=reason,
                        use_market=True
                    )
                    if result:
                        closed += 1
        
        logger.info(f"Закрыто {closed} позиций")
        return closed
    
    async def get_open_positions_summary(self) -> dict:
        """Получает сводку по открытым позициям"""
        with get_db() as db:
            positions = db.query(Position).filter(
                Position.status.in_([
                    PositionStatus.OPEN,
                    PositionStatus.PARTIAL_TP1,
                    PositionStatus.PARTIAL_TP2
                ])
            ).all()
        
        summary = {
            'count': len(positions),
            'total_value': 0,
            'unrealized_pnl': 0,
            'positions': []
        }
        
        for pos in positions:
            ticker = await self.exchange.get_ticker(pos.symbol)
            current_price = ticker['last'] if ticker else pos.entry_price
            
            current_value = pos.current_amount * current_price
            entry_value = pos.current_amount * pos.entry_price
            unrealized_pnl = current_value - entry_value
            pnl_pct = (current_price / pos.entry_price - 1) * 100
            
            summary['total_value'] += current_value
            summary['unrealized_pnl'] += unrealized_pnl
            summary['positions'].append({
                'id': pos.id,
                'symbol': pos.symbol,
                'entry_price': pos.entry_price,
                'current_price': current_price,
                'amount': pos.current_amount,
                'value_usdt': current_value,
                'pnl_pct': pnl_pct,
                'pnl_usdt': unrealized_pnl,
                'status': pos.status.value,
            })
        
        return summary
