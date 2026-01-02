"""
Telegram бот - отправка сигналов и обработка команд
"""
import asyncio
from typing import Dict, Optional
from datetime import datetime, timezone
from loguru import logger

from telegram import (
    Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputFile
)
from telegram.ext import Application, ContextTypes
from telegram.constants import ParseMode

from src.config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
from src.database import get_db, Signal, SignalStatus


class TelegramBot:
    """Telegram бот для отправки сигналов"""
    
    def __init__(self):
        self.token = TELEGRAM_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self.app: Optional[Application] = None
        self.bot: Optional[Bot] = None
        
    async def init(self):
        """Инициализация бота"""
        self.app = Application.builder().token(self.token).build()
        self.bot = self.app.bot
        logger.info("✅ Telegram бот инициализирован")
        
    async def send_signal(self, signal: Dict, signal_id: int) -> Optional[int]:
        """
        Отправляет сигнал в Telegram с кнопками.
        Возвращает message_id.
        """
        try:
            # Формируем текст сообщения
            text = self._format_signal_message(signal)
            
            # Кнопки
            keyboard = [
                [
                    InlineKeyboardButton(
                        "✅ Купить", 
                        callback_data=f"buy_{signal_id}"
                    ),
                    InlineKeyboardButton(
                        "⏭ Пропустить", 
                        callback_data=f"skip_{signal_id}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "📊 График", 
                        callback_data=f"chart_{signal['symbol'].replace('/', '_')}"
                    ),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Отправляем
            message = await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            
            # Сохраняем message_id в БД
            with get_db() as db:
                db_signal = db.query(Signal).filter(Signal.id == signal_id).first()
                if db_signal:
                    db_signal.message_id = message.message_id
            
            logger.info(f"📤 Сигнал {signal['symbol']} отправлен (msg_id: {message.message_id})")
            return message.message_id
            
        except Exception as e:
            logger.error(f"Ошибка отправки сигнала: {e}")
            return None
    
    def _format_signal_message(self, signal: Dict) -> str:
        """Форматирует сообщение сигнала"""
        levels = signal["levels"]
        
        # Эмодзи для структуры EMA
        ema_structure = "✅" if (
            signal["ema7"] > signal["ema14"] > signal["ema28"]
        ) else "⚠️"
        
        above_ema100 = "✅" if signal["price"] > signal["ema100"] else "❌"
        
        text = f"""
🚀 <b>POTENTIAL BREAKOUT (SPOT)</b>

<b>Пара:</b> {signal['symbol']}
<b>Цена:</b> {signal['price']:.6f}
<b>Рост свечи:</b> +{signal['candle_growth_pct']:.1f}%
<b>Объём:</b> x{signal['volume_ratio']:.1f}
<b>Спред:</b> {signal['spread_pct']:.2f}%
<b>RSI:</b> {signal['rsi']:.0f}

📊 <b>Структура</b>
• EMA7 > EMA14 > EMA28: {ema_structure}
• Цена выше EMA100: {above_ema100}
• Накопление: ✅ (range {signal['accumulation_range']:.1f}x ATR)

📍 <b>Уровни</b>
• Entry: {levels['entry_low']:.6f} – {levels['entry_high']:.6f}
• Stop: {levels['stop_loss']:.6f} ({levels['risk_pct']:.1f}%)
• TP1: {levels['tp1']:.6f} (+5%)
• TP2: {levels['tp2']:.6f} (+10%)
• TP3: {levels['tp3']:.6f} (+15%)
• R/R: 1:{levels['rr_ratio']:.1f}

⏰ <i>{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC</i>
"""
        return text.strip()
    
    async def send_message(self, text: str, reply_markup=None) -> Optional[int]:
        """Отправляет обычное сообщение"""
        try:
            message = await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            return message.message_id
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения: {e}")
            return None
    
    async def edit_signal_message(
        self, 
        message_id: int, 
        new_status: str,
        extra_text: str = ""
    ):
        """Редактирует сообщение сигнала после решения"""
        try:
            status_emoji = {
                "accepted": "✅ ПРИНЯТ",
                "rejected": "❌ ПРОПУЩЕН",
                "executed": "🎯 ИСПОЛНЕН",
                "expired": "⏰ ИСТЁК",
            }
            
            # Добавляем статус в конец сообщения
            status_text = status_emoji.get(new_status, new_status.upper())
            
            await self.bot.edit_message_reply_markup(
                chat_id=self.chat_id,
                message_id=message_id,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{status_text} {extra_text}", callback_data="noop")]
                ])
            )
        except Exception as e:
            logger.error(f"Ошибка редактирования сообщения: {e}")
    
    async def send_position_update(self, position_data: Dict):
        """Отправляет обновление по позиции"""
        text = f"""
📊 <b>Обновление позиции</b>

<b>Пара:</b> {position_data['symbol']}
<b>Вход:</b> {position_data['entry_price']:.6f}
<b>Текущая:</b> {position_data['current_price']:.6f}
<b>P&L:</b> {position_data['pnl_pct']:+.2f}% (${position_data['pnl_usdt']:+.2f})

<b>Статус:</b> {position_data['status']}
"""
        await self.send_message(text)
    
    async def send_trade_executed(self, trade_data: Dict):
        """Отправляет уведомление об исполненной сделке"""
        side_emoji = "🟢" if trade_data['side'] == "BUY" else "🔴"
        
        text = f"""
{side_emoji} <b>Сделка исполнена</b>

<b>Пара:</b> {trade_data['symbol']}
<b>Тип:</b> {trade_data['side']}
<b>Цена:</b> {trade_data['price']:.6f}
<b>Объём:</b> {trade_data['amount']:.4f} (~${trade_data['value_usdt']:.2f})
<b>Причина:</b> {trade_data.get('reason', 'SIGNAL')}
"""
        
        if trade_data.get('pnl_usdt') is not None:
            pnl_emoji = "✅" if trade_data['pnl_usdt'] >= 0 else "❌"
            text += f"\n<b>P&L:</b> {pnl_emoji} {trade_data['pnl_pct']:+.2f}% (${trade_data['pnl_usdt']:+.2f})"
        
        await self.send_message(text)
    
    async def send_daily_summary(self, stats: Dict):
        """Отправляет дневную сводку"""
        pnl_emoji = "🟢" if stats['total_pnl'] >= 0 else "🔴"
        winrate = (
            stats['trades_won'] / (stats['trades_won'] + stats['trades_lost']) * 100
            if (stats['trades_won'] + stats['trades_lost']) > 0 else 0
        )
        
        text = f"""
📈 <b>Итоги дня</b>

<b>Сигналов:</b> {stats['signals_sent']}
• Принято: {stats['signals_accepted']}
• Пропущено: {stats['signals_rejected']}

<b>Сделок:</b> {stats['trades_won'] + stats['trades_lost']}
• Прибыльных: {stats['trades_won']} ✅
• Убыточных: {stats['trades_lost']} ❌
• Winrate: {winrate:.0f}%

<b>P&L:</b> {pnl_emoji} ${stats['total_pnl']:+.2f}
"""
        await self.send_message(text)
