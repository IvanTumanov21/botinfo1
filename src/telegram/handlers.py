"""
Обработчики команд и кнопок Telegram
"""
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from loguru import logger

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    ContextTypes
)

from src.config import TELEGRAM_CHAT_ID, RISK_MANAGEMENT
from src.database import (
    get_db, Signal, SignalStatus, Position, PositionStatus,
    Trade, DailyStats
)

if TYPE_CHECKING:
    from src.exchange import BybitExchange
    from src.telegram import TelegramBot


# Глобальные ссылки на компоненты (устанавливаются при старте)
exchange: "BybitExchange" = None
telegram_bot: "TelegramBot" = None


def set_components(ex: "BybitExchange", tg: "TelegramBot"):
    """Устанавливает ссылки на компоненты"""
    global exchange, telegram_bot
    exchange = ex
    telegram_bot = tg


def is_authorized(update: Update) -> bool:
    """Проверяет авторизацию пользователя"""
    user_id = None
    if update.message:
        user_id = update.message.from_user.id
    elif update.callback_query:
        user_id = update.callback_query.from_user.id
    return user_id == TELEGRAM_CHAT_ID


# ================== КОМАНДЫ ==================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start - главное меню"""
    if not is_authorized(update):
        return
    
    keyboard = [
        [
            InlineKeyboardButton("📊 Статус", callback_data="status"),
            InlineKeyboardButton("💰 Баланс", callback_data="balance"),
        ],
        [
            InlineKeyboardButton("📈 Позиции", callback_data="positions"),
            InlineKeyboardButton("📋 История", callback_data="history"),
        ],
        [
            InlineKeyboardButton("⚙️ Сканирование", callback_data="scan_toggle"),
        ],
    ]
    
    text = """
🤖 <b>Breakout Scanner Bot</b>

Бот сканирует рынок и присылает сигналы.
<b>Ты — финальный фильтр!</b>

Нажми кнопку для управления:
"""
    
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /status - текущий статус бота"""
    if not is_authorized(update):
        return
    
    with get_db() as db:
        # Открытые позиции
        open_positions = db.query(Position).filter(
            Position.status.in_([
                PositionStatus.OPEN,
                PositionStatus.PARTIAL_TP1,
                PositionStatus.PARTIAL_TP2
            ])
        ).count()
        
        # Ожидающие сигналы
        pending_signals = db.query(Signal).filter(
            Signal.status == SignalStatus.PENDING
        ).count()
        
        # Сегодняшняя статистика
        today = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        stats = db.query(DailyStats).filter(
            DailyStats.date == today
        ).first()
    
    # BTC изменение
    btc_change = 0.0
    if exchange:
        btc_change = await exchange.get_btc_change_1h()
    
    btc_emoji = "🟢" if btc_change >= 0 else "🔴"
    
    text = f"""
📊 <b>Статус бота</b>

<b>Рынок:</b>
• BTC (1ч): {btc_emoji} {btc_change*100:+.2f}%

<b>Позиции:</b> {open_positions}/{RISK_MANAGEMENT['max_positions']}
<b>Ожидают решения:</b> {pending_signals}

<b>Сегодня:</b>
• Сигналов: {stats.signals_sent if stats else 0}
• Принято: {stats.signals_accepted if stats else 0}
• Стопов: {stats.stop_losses_today if stats else 0}
• P&L: ${stats.total_pnl_usdt if stats else 0:+.2f}
"""
    
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /positions - открытые позиции"""
    if not is_authorized(update):
        return
    
    with get_db() as db:
        positions = db.query(Position).filter(
            Position.status.in_([
                PositionStatus.OPEN,
                PositionStatus.PARTIAL_TP1,
                PositionStatus.PARTIAL_TP2
            ])
        ).all()
    
    if not positions:
        await update.message.reply_text("📭 Нет открытых позиций")
        return
    
    text = "📈 <b>Открытые позиции</b>\n\n"
    
    keyboard = []
    
    for pos in positions:
        # Получаем текущую цену
        current_price = pos.entry_price
        if exchange:
            ticker = await exchange.get_ticker(pos.symbol)
            if ticker:
                current_price = ticker['last']
        
        pnl_pct = (current_price / pos.entry_price - 1) * 100
        pnl_emoji = "🟢" if pnl_pct >= 0 else "🔴"
        
        text += f"""
<b>{pos.symbol}</b>
• Вход: {pos.entry_price:.6f}
• Текущая: {current_price:.6f}
• P&L: {pnl_emoji} {pnl_pct:+.2f}%
• SL: {pos.stop_loss:.6f} | TP1: {pos.tp1:.6f}
• Статус: {pos.status.value}
"""
        
        keyboard.append([
            InlineKeyboardButton(
                f"❌ Закрыть {pos.symbol.split('/')[0]}", 
                callback_data=f"close_{pos.id}"
            )
        ])
    
    keyboard.append([
        InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")
    ])
    
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ================== ОБРАБОТЧИКИ КНОПОК ==================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик всех inline кнопок"""
    if not is_authorized(update):
        return
    
    query = update.callback_query
    await query.answer()
    data = query.data
    
    # Кнопка "Купить"
    if data.startswith("buy_"):
        signal_id = int(data.split("_")[1])
        await handle_buy_signal(query, signal_id)
    
    # Кнопка "Пропустить"
    elif data.startswith("skip_"):
        signal_id = int(data.split("_")[1])
        await handle_skip_signal(query, signal_id)
    
    # Кнопка "График"
    elif data.startswith("chart_"):
        symbol = data.split("_", 1)[1].replace("_", "/")
        await handle_chart(query, symbol)
    
    # Кнопка "Закрыть позицию"
    elif data.startswith("close_"):
        position_id = int(data.split("_")[1])
        await handle_close_position(query, position_id)
    
    # Статус
    elif data == "status":
        await handle_status(query)
    
    # Баланс
    elif data == "balance":
        await handle_balance(query)
    
    # Позиции
    elif data == "positions":
        await handle_positions_list(query)
    
    # История
    elif data == "history":
        await handle_history(query)
    
    # Назад в главное меню
    elif data == "back_to_main":
        await handle_back_to_main(query)
    
    # noop - ничего не делаем
    elif data == "noop":
        pass


async def handle_buy_signal(query, signal_id: int):
    """Обработка нажатия кнопки "Купить" """
    with get_db() as db:
        signal = db.query(Signal).filter(Signal.id == signal_id).first()
        
        if not signal:
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Сигнал не найден", callback_data="noop")]
                ])
            )
            return
        
        if signal.status != SignalStatus.PENDING:
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"⚠️ Уже обработан: {signal.status.value}", callback_data="noop")]
                ])
            )
            return
        
        # Проверяем, не ушла ли цена
        current_price = signal.price
        if exchange:
            ticker = await exchange.get_ticker(signal.symbol)
            if ticker:
                current_price = ticker['last']
        
        price_diff = (current_price - signal.price) / signal.price
        
        if price_diff > 0.01:  # Цена ушла больше чем на 1%
            signal.status = SignalStatus.EXPIRED
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"⏰ Цена ушла (+{price_diff*100:.1f}%)", callback_data="noop")]
                ])
            )
            return
        
        # Обновляем статус
        signal.status = SignalStatus.ACCEPTED
        signal.decided_at = datetime.now(timezone.utc)
        
        # Обновляем дневную статистику
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        stats = db.query(DailyStats).filter(DailyStats.date == today).first()
        if stats:
            stats.signals_accepted += 1
    
    # Редактируем сообщение
    await query.edit_message_reply_markup(
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ ПРИНЯТ - Исполняется...", callback_data="noop")]
        ])
    )
    
    # Исполняем ордер (будет в order_executor)
    # Здесь вызов execute_buy_order(signal)
    logger.info(f"✅ Сигнал {signal_id} принят, создаём ордер")
    
    # Отправляем подтверждение
    if telegram_bot:
        await telegram_bot.send_message(
            f"📝 Создаю лимитный ордер на покупку {signal.symbol}...\n"
            f"Цена входа: {signal.entry_price:.6f}"
        )


async def handle_skip_signal(query, signal_id: int):
    """Обработка нажатия кнопки "Пропустить" """
    with get_db() as db:
        signal = db.query(Signal).filter(Signal.id == signal_id).first()
        
        if not signal:
            return
        
        if signal.status != SignalStatus.PENDING:
            return
        
        signal.status = SignalStatus.REJECTED
        signal.decided_at = datetime.now(timezone.utc)
        
        # Обновляем статистику
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        stats = db.query(DailyStats).filter(DailyStats.date == today).first()
        if stats:
            stats.signals_rejected += 1
    
    await query.edit_message_reply_markup(
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭ ПРОПУЩЕН", callback_data="noop")]
        ])
    )
    
    logger.info(f"⏭ Сигнал {signal_id} пропущен")


async def handle_chart(query, symbol: str):
    """Отправка графика"""
    # TODO: Генерация графика через mplfinance
    await query.message.reply_text(
        f"📊 График {symbol}\n\n"
        f"<i>Функция в разработке</i>",
        parse_mode="HTML"
    )


async def handle_close_position(query, position_id: int):
    """Закрытие позиции вручную"""
    with get_db() as db:
        position = db.query(Position).filter(Position.id == position_id).first()
        
        if not position:
            await query.edit_message_text("❌ Позиция не найдена")
            return
        
        if position.status not in [
            PositionStatus.OPEN, 
            PositionStatus.PARTIAL_TP1, 
            PositionStatus.PARTIAL_TP2
        ]:
            await query.edit_message_text(f"⚠️ Позиция уже закрыта: {position.status.value}")
            return
    
    # Закрываем позицию (будет в order_executor)
    logger.info(f"🔴 Закрываем позицию {position_id} вручную")
    
    await query.edit_message_text(
        f"🔄 Закрываю позицию {position.symbol}...\n"
        f"<i>Создаю маркет ордер на продажу</i>",
        parse_mode="HTML"
    )


async def handle_status(query):
    """Показать статус"""
    with get_db() as db:
        open_positions = db.query(Position).filter(
            Position.status.in_([
                PositionStatus.OPEN,
                PositionStatus.PARTIAL_TP1,
                PositionStatus.PARTIAL_TP2
            ])
        ).count()
        
        pending_signals = db.query(Signal).filter(
            Signal.status == SignalStatus.PENDING
        ).count()
        
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        stats = db.query(DailyStats).filter(DailyStats.date == today).first()
    
    btc_change = 0.0
    if exchange:
        btc_change = await exchange.get_btc_change_1h()
    
    btc_emoji = "🟢" if btc_change >= 0 else "🔴"
    
    text = f"""
📊 <b>Статус бота</b>

<b>BTC:</b> {btc_emoji} {btc_change*100:+.2f}% (1ч)
<b>Позиции:</b> {open_positions}/{RISK_MANAGEMENT['max_positions']}
<b>Ожидают:</b> {pending_signals}

<b>Сегодня:</b>
• Сигналов: {stats.signals_sent if stats else 0}
• P&L: ${stats.total_pnl_usdt if stats else 0:+.2f}
"""
    
    await query.edit_message_text(text, parse_mode="HTML")


async def handle_balance(query):
    """Показать баланс"""
    if not exchange:
        await query.edit_message_text("❌ Биржа не подключена")
        return
    
    balance = await exchange.get_balance()
    
    usdt = balance.get('USDT', {})
    
    text = f"""
💰 <b>Баланс</b>

<b>USDT:</b>
• Свободно: {usdt.get('free', 0):.2f}
• В ордерах: {usdt.get('used', 0):.2f}
• Всего: {usdt.get('total', 0):.2f}
"""
    
    # Добавляем другие активы
    for currency, data in balance.items():
        if currency != 'USDT' and data.get('total', 0) > 0:
            text += f"\n<b>{currency}:</b> {data.get('total', 0):.6f}"
    
    keyboard = [
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]
    ]
    
    await query.edit_message_text(
        text, 
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_positions_list(query):
    """Список позиций"""
    with get_db() as db:
        positions = db.query(Position).filter(
            Position.status.in_([
                PositionStatus.OPEN,
                PositionStatus.PARTIAL_TP1,
                PositionStatus.PARTIAL_TP2
            ])
        ).all()
    
    if not positions:
        text = "📭 Нет открытых позиций"
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]]
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    text = "📈 <b>Открытые позиции</b>\n"
    keyboard = []
    
    for pos in positions:
        current_price = pos.entry_price
        if exchange:
            ticker = await exchange.get_ticker(pos.symbol)
            if ticker:
                current_price = ticker['last']
        
        pnl_pct = (current_price / pos.entry_price - 1) * 100
        pnl_emoji = "🟢" if pnl_pct >= 0 else "🔴"
        
        text += f"\n{pnl_emoji} <b>{pos.symbol}</b>: {pnl_pct:+.1f}%"
        
        keyboard.append([
            InlineKeyboardButton(
                f"❌ Закрыть {pos.symbol.split('/')[0]}", 
                callback_data=f"close_{pos.id}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")])
    
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_history(query):
    """История сделок"""
    with get_db() as db:
        trades = db.query(Trade).order_by(
            Trade.created_at.desc()
        ).limit(10).all()
    
    if not trades:
        text = "📭 История пуста"
    else:
        text = "📋 <b>Последние сделки</b>\n"
        
        for trade in trades:
            side_emoji = "🟢" if trade.side == "BUY" else "🔴"
            pnl_text = ""
            if trade.pnl_usdt is not None:
                pnl_emoji = "✅" if trade.pnl_usdt >= 0 else "❌"
                pnl_text = f" | {pnl_emoji} ${trade.pnl_usdt:+.2f}"
            
            text += f"\n{side_emoji} {trade.symbol} @ {trade.price:.6f}{pnl_text}"
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]]
    
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_back_to_main(query):
    """Возврат в главное меню"""
    keyboard = [
        [
            InlineKeyboardButton("📊 Статус", callback_data="status"),
            InlineKeyboardButton("💰 Баланс", callback_data="balance"),
        ],
        [
            InlineKeyboardButton("📈 Позиции", callback_data="positions"),
            InlineKeyboardButton("📋 История", callback_data="history"),
        ],
    ]
    
    text = "🤖 <b>Breakout Scanner Bot</b>\n\nВыбери действие:"
    
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


def setup_handlers(app: Application):
    """Регистрация всех обработчиков"""
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    logger.info("✅ Обработчики Telegram зарегистрированы")
