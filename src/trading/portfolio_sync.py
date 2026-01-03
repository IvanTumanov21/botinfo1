import asyncio
from datetime import datetime, timezone
from typing import Dict, List
from loguru import logger

from src.database import get_db, Position, PositionStatus
from src.database.models import Holding
from src.exchange import BybitExchange

SYNC_INTERVAL_SEC = 60  # проверяем баланс раз в минуту


async def sync_holdings(exchange: BybitExchange):
    """Синхронизирует актуальные активы с биржи в таблицу holdings."""
    if not exchange:
        return

    try:
        balances = await exchange.get_balance()
    except Exception as e:
        logger.error(f"Ошибка получения баланса при синхронизации: {e}")
        return

    if not balances:
        return

    # Загружаем открытые позиции бота, чтобы использовать их входные цены
    with get_db() as db:
        positions = db.query(Position).filter(
            Position.status.in_([
                PositionStatus.OPEN,
                PositionStatus.PARTIAL_TP1,
                PositionStatus.PARTIAL_TP2,
            ])
        ).all()
        pos_map = {p.symbol: p for p in positions}

        holdings: Dict[str, Holding] = {
            h.symbol: h for h in db.query(Holding).all()
        }

        seen_symbols: List[str] = []

        for currency, data in balances.items():
            if currency == "USDT":
                continue

            amount = float(data.get("total", 0) or 0)
            if amount <= 0:
                continue

            symbol = f"{currency}/USDT"
            seen_symbols.append(symbol)

            # Получаем текущую цену
            ticker = await exchange.get_ticker(symbol)
            if not ticker or not ticker.get("last"):
                continue
            last_price = float(ticker["last"])

            # Определяем среднюю цену входа
            pos = pos_map.get(symbol)
            if pos:
                avg_entry = float(pos.entry_price)
                amount_for_avg = float(pos.current_amount)
            else:
                existing = holdings.get(symbol)
                # Если добавили актив вручную и это первое обнаружение,
                # принимаем цену на момент обнаружения как вход
                if not existing:
                    avg_entry = last_price
                    amount_for_avg = amount
                else:
                    # Если количество выросло — обновляем среднюю по взвешенной цене
                    prev_amount = existing.amount or 0.0
                    if amount > prev_amount + 1e-9:
                        delta = amount - prev_amount
                        avg_entry = (
                            prev_amount * (existing.avg_entry_price or last_price)
                            + delta * last_price
                        ) / amount
                    else:
                        avg_entry = existing.avg_entry_price or last_price
                    amount_for_avg = amount

            current_value = amount * last_price

            if symbol in holdings:
                h = holdings[symbol]
                h.amount = amount_for_avg
                h.avg_entry_price = avg_entry
                h.last_price = last_price
                h.last_value_usdt = current_value
                h.updated_at = datetime.utcnow()
            else:
                h = Holding(
                    symbol=symbol,
                    amount=amount_for_avg,
                    avg_entry_price=avg_entry,
                    last_price=last_price,
                    last_value_usdt=current_value,
                )
                db.add(h)

        # Удаляем записи по активам, которых больше нет на балансе
        for sym, h in holdings.items():
            if sym not in seen_symbols:
                db.delete(h)

        db.commit()


async def portfolio_loop(exchange: BybitExchange):
    """Фоновая задача синхронизации портфеля каждые SYNC_INTERVAL_SEC секунд."""
    while True:
        try:
            await sync_holdings(exchange)
            await asyncio.sleep(SYNC_INTERVAL_SEC)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Ошибка в portfolio_loop: {e}")
            await asyncio.sleep(10)
