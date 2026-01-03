"""
Функции для отправки уведомлений о предсигналах в Telegram
"""
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime
from loguru import logger

async def send_presignal_notification(
    bot: Bot,
    chat_id: int,
    symbol: str,
    presignal_data: dict,
    price: float
):
    """
    Отправляет красивое уведомление о паре, близкой к сигналу.
    
    Args:
        bot: Telegram bot instance
        chat_id: ID чата для отправки
        symbol: Название пары (например BTCUSDT)
        presignal_data: Результат detect_presignals()
        price: Текущая цена
    """
    try:
        details = presignal_data.get("details", {})
        
        # Формируем статус каждого условия
        conditions = {
            "Volume": presignal_data.get("volume_ok", False),
            "Growth": presignal_data.get("growth_ok", False),
            "RSI": presignal_data.get("rsi_ok", False),
            "EMA setup": presignal_data.get("ema_setup_ok", False),
        }
        
        conditions_met = presignal_data.get("conditions_met", 0)
        
        # Формируем сообщение
        time_str = datetime.utcnow().strftime("%H:%M:%S UTC")
        
        text = f"""
🎯 <b>ПРЕДСИГНАЛ</b> | {conditions_met}/4 условий

<b>{symbol}</b>
💰 ${price:.6f}
🕐 {time_str}

<b>Условия:</b>
"""
        
        # Добавляем статус каждого условия
        if conditions["Volume"]:
            text += f"\n✅ Volume spike - {details.get('volume_ratio', 0):.1f}x (нужно {details.get('volume_min_required', 3)}x)"
        else:
            text += f"\n❌ Volume spike - {details.get('volume_ratio', 0):.1f}x (нужно {details.get('volume_min_required', 3)}x)"
        
        if conditions["Growth"]:
            text += f"\n✅ Рост свечи - +{details.get('candle_growth', 0):.2f}% (диапазон {details.get('growth_min', 0.5)}%-{details.get('growth_max', 8)}%)"
        else:
            text += f"\n❌ Рост свечи - +{details.get('candle_growth', 0):.2f}% (диапазон {details.get('growth_min', 0.5)}%-{details.get('growth_max', 8)}%)"
        
        if conditions["RSI"]:
            text += f"\n✅ RSI в импульсной зоне - {details.get('rsi', 0):.0f} (нужно {details.get('rsi_min', 50)}-{details.get('rsi_max', 70)})"
        else:
            text += f"\n❌ RSI в импульсной зоне - {details.get('rsi', 0):.0f} (нужно {details.get('rsi_min', 50)}-{details.get('rsi_max', 70)})"
        
        if conditions["EMA setup"]:
            ema9_ok = details.get("ema9_above_ema21", False)
            price_ok = details.get("price_above_ema50", False)
            text += f"\n✅ EMA структура - EMA9>21: {ema9_ok}, Price>EMA50: {price_ok}"
        else:
            ema9_ok = details.get("ema9_above_ema21", False)
            price_ok = details.get("price_above_ema50", False)
            text += f"\n❌ EMA структура - EMA9>21: {ema9_ok}, Price>EMA50: {price_ok}"
        
        text += f"""

<b>Что дальше?</b>
Осталось выполнить условия для полного сигнала.
Наблюдаем... 👀
"""
        
        # Inline кнопки
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📊 График", url=f"https://www.bybit.com/en/trade/spot/{symbol}"),
                InlineKeyboardButton("🔔 Мониторить", callback_data=f"monitor_{symbol}")
            ]
        ])
        
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
        
        logger.info(f"📤 Отправлено уведомление о предсигнале: {symbol} ({conditions_met}/4 условий)")
        
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления о предсигнале: {e}")


async def send_presignal_status_message(
    bot: Bot,
    chat_id: int,
    presignals_list: list
):
    """
    Отправляет сводное сообщение о всех найденных предсигналах.
    
    Args:
        bot: Telegram bot instance
        chat_id: ID чата
        presignals_list: Список найденных предсигналов
    """
    try:
        if not presignals_list:
            text = "😴 <b>Нет предсигналов</b>\n\nРынок ждёт момента для пампов..."
        else:
            text = f"🔍 <b>Найдено {len(presignals_list)} предсигналов</b>\n\n"
            
            # Группируем по количеству выполненных условий
            by_count = {}
            for ps in presignals_list:
                count = ps.get("conditions_met", 0)
                if count not in by_count:
                    by_count[count] = []
                by_count[count].append(ps)
            
            # Выводим от большего количества условий к меньшему
            for count in sorted(by_count.keys(), reverse=True):
                items = by_count[count]
                text += f"\n<b>{count}/4 условия ({len(items)} пар)</b>\n"
                
                for ps in items[:5]:  # Показываем первые 5
                    symbol = ps.get("symbol", "?")
                    price = ps.get("price", 0)
                    text += f"  • {symbol} (${price:.6f})\n"
                
                if len(items) > 5:
                    text += f"  ... и ещё {len(items) - 5}\n"
        
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Ошибка отправки статуса предсигналов: {e}")
