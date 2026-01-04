"""
Обработчики команд и кнопок Telegram
"""
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional
from loguru import logger
import html

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    ContextTypes
)

from src.config import TELEGRAM_CHAT_ID, RISK_MANAGEMENT, PRESIGNALS
from src.database import (
    get_db, Signal, SignalStatus, Position, PositionStatus,
    Trade, DailyStats, Holding
)
from src.trading.portfolio_sync import sync_holdings

if TYPE_CHECKING:
    from src.exchange import BybitExchange
    from src.telegram import TelegramBot


# Глобальные ссылки на компоненты (устанавливаются при старте)
exchange: "BybitExchange" = None
telegram_bot: "TelegramBot" = None

# Временное хранилище для ожидания ввода суммы {user_id: signal_id}
pending_custom_amounts = {}


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
    user_id = None
    if update.message:
        user_id = update.message.from_user.id
        logger.info(f"📍 Команда /start от {user_id} (ожидается {TELEGRAM_CHAT_ID})")
    
    # Проверяем авторизацию
    if not is_authorized(update):
        logger.warning(f"❌ Неавторизованный /start: {user_id}")
        try:
            await update.message.reply_text(
                f"❌ Доступ запрещён.\nВаш ID: {user_id}"
            )
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения об ошибке: {e}")
        return
    
    # Получаем статус сканирования и предсигналов из БД
    from src.database.models import BotSettings
    
    with get_db() as db:
        scan_setting = db.query(BotSettings).filter(BotSettings.key == "scan_enabled").first()
        presignals_setting = db.query(BotSettings).filter(BotSettings.key == "presignals_enabled").first()
        
        scan_enabled = scan_setting and scan_setting.value.lower() == "true"
        presignals_enabled = presignals_setting and presignals_setting.value.lower() == "true"
    
    scan_status = "🟢 Включено" if scan_enabled else "🔴 Выключено"
    presignals_status = "🟢 Включено" if presignals_enabled else "🔴 Выключено"
    
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
            InlineKeyboardButton("🎯 Предсигналы", callback_data="presignals_menu"),
        ],
    ]
    
    text = f"""
🤖 <b>Breakout Scanner Bot</b>

Бот сканирует рынок и присылает сигналы.
<b>Ты — финальный фильтр!</b>

<b>Статус:</b>
⚙️ Сканирование: {scan_status}
🎯 Предсигналы: {presignals_status}

Нажми кнопку для управления:
"""
    
    try:
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        logger.info(f"✅ Меню отправлено пользователю {user_id}")

    except Exception as e:
        logger.error(f"❌ Ошибка отправки меню: {e}")
        try:
            await update.message.reply_text(f"❌ Ошибка: {str(e)[:100]}")
        except:
            pass


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
    
    # Кнопка "Купить" (выбор суммы) или конкретная сумма
    if data.startswith("buy_amt_"):
        _, _, signal_id_str, amount_str = data.split("_", 3)
        signal_id = int(signal_id_str)
        if amount_str == "auto":
            amount_usdt = None
        elif amount_str == "custom":
            amount_usdt = -1  # специальный маркер для запроса ввода
        else:
            amount_usdt = float(amount_str)
        await handle_buy_signal(query, signal_id, amount_usdt)
    elif data.startswith("buy_"):
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
    
    # Сканирование меню
    elif data == "scan_toggle":
        await handle_scan_toggle(query)
    
    # Переключение сканирования
    elif data == "scan_toggle_switch":
        await handle_scan_toggle_switch(query)
    
    # Меню предсигналов
    elif data == "presignals_menu":
        await handle_presignals_menu(query)
    
    # Переключение предсигналов
    elif data == "presignals_toggle":
        await handle_presignals_toggle(query)
    
    # Список предсигналов
    elif data == "presignals_list":
        await query.edit_message_text(
            "📊 <b>Текущие предсигналы</b>\n\n"
            "Скоро здесь появится список активных предсигналов...",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="presignals_menu")]])
        )
    
    # Интервал предсигналов
    elif data == "presignals_interval":
        await handle_presignals_interval(query)
    
    # Установка интервала
    elif data.startswith("presignals_interval_set_"):
        interval = int(data.split("_")[-1])
        await handle_presignals_interval_set(query, interval)
    
    # Принудительный скан
    elif data == "force_scan":
        await handle_force_scan(query)
    
    # Вернуться в главное меню
    elif data == "back_to_main":
        await handle_back_to_main(query)
    elif data == "force_scan":
        await handle_force_scan(query)
    
    # Назад в главное меню
    elif data == "back_to_main":
        await handle_back_to_main(query)
    
    # noop - ничего не делаем
    elif data == "noop":
        pass


async def handle_buy_signal(query, signal_id: int, amount_usdt: Optional[float] = None):
    """Обработка нажатия кнопки "Купить" с выбором суммы"""
    try:
        # Если сумма не выбрана — показать быстрые кнопки
        if amount_usdt is None:
            keyboard = [
                [
                    InlineKeyboardButton("$10", callback_data=f"buy_amt_{signal_id}_10"),
                    InlineKeyboardButton("$25", callback_data=f"buy_amt_{signal_id}_25"),
                    InlineKeyboardButton("$50", callback_data=f"buy_amt_{signal_id}_50"),
                ],
                [
                    InlineKeyboardButton("💬 Своя сумма", callback_data=f"buy_amt_{signal_id}_custom"),
                    InlineKeyboardButton("🤖 Авто", callback_data=f"buy_amt_{signal_id}_auto"),
                ],
                [InlineKeyboardButton("🔙 Отмена", callback_data="noop")]
            ]
            await query.answer("Выбери сумму сделки", show_alert=False)
            await query.edit_message_reply_markup(InlineKeyboardMarkup(keyboard))
            return
        
        # Если выбрана "Своя сумма" - запрашиваем ввод
        if amount_usdt == -1:  # специальный маркер для custom
            user_id = query.from_user.id
            pending_custom_amounts[user_id] = signal_id
            
            await query.answer("Напиши сумму в USDT (например: 15)", show_alert=True)
            await query.edit_message_text(
                f"💬 <b>Введите сумму в USDT</b>\n\n"
                f"Напишите число в чат (например: 15 или 75.5)\n"
                f"После ввода будет выполнена покупка.\n\n"
                f"<i>Signal ID: {signal_id}</i>",
                parse_mode="HTML"
            )
            return
        
        # 1. Быстрое обновление статуса (должна быть асинхронной или не блокировать)
        with get_db() as db:
            signal = db.query(Signal).filter(Signal.id == signal_id).first()
            
            if not signal:
                await query.answer("❌ Сигнал не найден", show_alert=True)
                return
            
            if signal.status != SignalStatus.PENDING:
                await query.answer(f"⚠️ Уже обработан: {signal.status.value}", show_alert=True)
                return
            
            symbol = signal.symbol
            
            # Проверяем, не ушла ли цена
            current_price = signal.price
            if exchange:
                try:
                    ticker = await exchange.get_ticker(signal.symbol)
                    if ticker:
                        current_price = ticker['last']
                except:
                    pass
            
            price_diff = (current_price - signal.price) / signal.price
            
            if price_diff > 0.01:  # Цена ушла больше чем на 1%
                await query.answer(f"⏰ Цена ушла на +{price_diff*100:.2f}%", show_alert=True)
                signal.status = SignalStatus.EXPIRED
                db.commit()
                return
            
            # Обновляем статус в БД (быстро)
            signal.status = SignalStatus.ACCEPTED
            signal.decided_at = datetime.now(timezone.utc)
            
            # Обновляем дневную статистику
            today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            stats = db.query(DailyStats).filter(DailyStats.date == today).first()
            if stats:
                stats.signals_accepted += 1
            
            db.commit()
        
        # 2. Сразу отвечаем пользователю
        await query.answer("✅ Принимаю сигнал, создаю ордер...", show_alert=False)
        
        # 3. Редактируем сообщение (неблокирующее)
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ ПРИНЯТ", callback_data="noop")],
                [InlineKeyboardButton("🔄 Исполняется...", callback_data="noop")]
            ])
        )
        
        # 4. Создаём ордер в фоне (асинхронно)
        if exchange:
            from src.trading.executor import OrderExecutor
            executor = OrderExecutor(exchange)
            
            try:
                position = await executor.execute_buy_from_signal(signal_id, amount_usdt_override=amount_usdt)
                if position:
                    logger.info(f"✅ Позиция открыта: {position['symbol']} ID={position['id']}")
                    # Отправляем подтверждение
                    if telegram_bot:
                        await telegram_bot.send_message(
                            f"✅ <b>Ордер выполнен!</b>\n\n"
                            f"📍 {position['symbol']}\n"
                            f"💵 Вход: {position['entry_price']:.6f}\n"
                            f"📊 Объём: {position['entry_amount']:.6f}\n"
                            f"💰 Сумма: {position['entry_value_usdt']:.2f} USDT"
                        )
                else:
                    logger.error(f"❌ Не удалось открыть позицию для сигнала {signal_id}")
                    if telegram_bot:
                        await telegram_bot.send_message(
                            f"❌ Не удалось создать ордер на {symbol}\n"
                            f"Проверьте баланс и лимиты!"
                        )
            except Exception as e:
                logger.error(f"❌ Ошибка при создании позиции: {e}")
                if telegram_bot:
                    safe_err = html.escape(str(e))[:200]
                    await telegram_bot.send_message(
                        f"❌ Ошибка исполнения: {safe_err}"
                    )
    
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_buy_signal: {e}")
        await query.answer(f"❌ Ошибка: {str(e)[:50]}", show_alert=True)


async def handle_skip_signal(query, signal_id: int):
    """Обработка нажатия кнопки "Пропустить" """
    try:
        with get_db() as db:
            signal = db.query(Signal).filter(Signal.id == signal_id).first()
            
            if not signal or signal.status != SignalStatus.PENDING:
                return
            
            signal.status = SignalStatus.REJECTED
            signal.decided_at = datetime.now(timezone.utc)
            
            # Обновляем статистику
            today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            stats = db.query(DailyStats).filter(DailyStats.date == today).first()
            if stats:
                stats.signals_rejected += 1
            
            db.commit()
        
        # Быстрый ответ
        await query.answer("⏭ Сигнал пропущен", show_alert=False)
        
        # Асинхронно обновляем сообщение
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭ ПРОПУЩЕН", callback_data="noop")]
            ])
        )
        
        logger.info(f"⏭ Сигнал {signal_id} пропущен")
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_skip_signal: {e}")


async def handle_chart(query, symbol: str):
    """Отправка графика с информацией о паре"""
    try:
        # Быстрый ответ
        await query.answer("📊 Загружаю данные о паре...", show_alert=False)
        
        if not exchange:
            await query.edit_message_text(
                "❌ Биржа не подключена",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="noop")]])
            )
            return
        
        # Получаем данные с биржи
        ticker = await exchange.get_ticker(symbol)
        
        if not ticker:
            await query.edit_message_text(
                f"❌ Не удалось получить данные по {symbol}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="noop")]])
            )
            return
        
        last_price = ticker.get('last', 0)
        bid = ticker.get('bid', 0)
        ask = ticker.get('ask', 0)
        volume_24h = ticker.get('volume24h', 0)
        
        # Получаем исторические данные для изменения цены
        klines = await exchange.get_klines(symbol, "1h", limit=24)
        
        change_24h = 0
        if klines and len(klines) > 0:
            open_price = float(klines[0][1])
            change_24h = ((last_price - open_price) / open_price) * 100
        
        change_emoji = "🟢" if change_24h >= 0 else "🔴"
        
        text = f"""
📊 <b>{symbol}</b>

<b>Цена:</b>
• Текущая: {last_price:.8g}
• Bid: {bid:.8g}
• Ask: {ask:.8g}

<b>Изменение:</b>
{change_emoji} {change_24h:+.2f}% (24ч)

<b>Объём:</b>
{volume_24h:.2f} USDT (24ч)
"""
        
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="noop")]]
        
        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        logger.info(f"📊 Запрос графика {symbol}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_chart: {e}")
        await query.answer(f"❌ Ошибка: {str(e)[:50]}", show_alert=True)


async def handle_close_position(query, position_id: int):
    """Закрытие позиции вручную"""
    try:
        with get_db() as db:
            position = db.query(Position).filter(Position.id == position_id).first()
            
            if not position:
                await query.answer("❌ Позиция не найдена", show_alert=True)
                return
            
            if position.status not in [
                PositionStatus.OPEN, 
                PositionStatus.PARTIAL_TP1, 
                PositionStatus.PARTIAL_TP2
            ]:
                await query.answer(f"⚠️ Уже закрыта: {position.status.value}", show_alert=True)
                return
            
            symbol = position.symbol
            amount = position.current_amount
        
        # Быстрый ответ
        await query.answer("✅ Закрываю позицию...", show_alert=False)
        
        # Закрываем позицию в фоне
        if exchange:
            from src.trading.executor import OrderExecutor
            executor = OrderExecutor(exchange)
            
            try:
                trade = await executor.execute_sell(
                    position_id=position_id,
                    amount=amount,
                    reason="MANUAL",
                    use_market=True
                )
                
                if trade:
                    logger.info(f"✅ Позиция {symbol} закрыта вручную")
                    if telegram_bot:
                        await telegram_bot.send_message(
                            f"✅ <b>Позиция закрыта!</b>\n\n"
                            f"📍 {symbol}\n"
                            f"💵 Цена выхода: {trade['price']:.6f}\n"
                            f"📊 Объём: {trade['amount']:.6f}\n"
                            f"💰 P&L: {trade['pnl_pct']:+.2f}% (${trade['pnl_usdt']:+.2f})"
                        )
                else:
                    # Проверяем, может позиция уже закрыта
                    with get_db() as db:
                        position = db.query(Position).filter(Position.id == position_id).first()
                        if position and position.status in [
                            PositionStatus.CLOSED_MANUAL,
                            PositionStatus.CLOSED_TP,
                            PositionStatus.CLOSED_SL
                        ]:
                            logger.info(f"ℹ️ Позиция {symbol} уже была закрыта ({position.status.value})")
                            if telegram_bot:
                                await telegram_bot.send_message(
                                    f"ℹ️ <b>Позиция уже закрыта</b>\n\n"
                                    f"📍 {symbol}\n"
                                    f"Статус: {position.status.value}\n"
                                    f"Причина: {position.close_reason or 'неизвестно'}"
                                )
                        else:
                            logger.error(f"❌ Не удалось закрыть позицию {position_id}")
                            if telegram_bot:
                                await telegram_bot.send_message(
                                    f"❌ Не удалось закрыть позицию {symbol}\n"
                                    f"Возможно недостаточно баланса или актив уже продан"
                                )
            except Exception as e:
                logger.error(f"❌ Ошибка при закрытии: {e}")
                if telegram_bot:
                    safe_err = html.escape(str(e))[:200]
                    await telegram_bot.send_message(f"❌ Ошибка: {safe_err}")
    
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_close_position: {e}")
        await query.answer(f"❌ Ошибка: {str(e)[:50]}", show_alert=True)


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
        # Сохраняем значения до выхода из сессии, чтобы избежать DetachedInstanceError
        stats_signals_sent = stats.signals_sent if stats else 0
        stats_total_pnl_usdt = stats.total_pnl_usdt if stats else 0.0
    
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
• Сигналов: {stats_signals_sent}
• P&L: ${stats_total_pnl_usdt:+.2f}
"""
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]]
    
    await query.edit_message_text(
        text, 
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_balance(query):
    """Показать баланс"""
    try:
        if not exchange:
            await query.edit_message_text("❌ Биржа не подключена")
            return
        
        # Обновляем данные в БД, чтобы поймать ручные покупки
        await sync_holdings(exchange)
        
        balance = await exchange.get_balance()
        usdt = balance.get('USDT', {})
        
        # Загружаем актуальные холдинги и сохраняем данные до закрытия сессии
        with get_db() as db:
            holdings = db.query(Holding).all()
            # Сохраняем данные в список словарей
            holdings_data = [
                {
                    'symbol': h.symbol,
                    'amount': h.amount,
                    'avg_entry_price': h.avg_entry_price,
                    'last_price': h.last_price
                }
                for h in holdings
            ]
        
        total_portfolio_usdt = 0.0
        total_pnl_usdt = 0.0
        assets_lines = ""
        
        for h_data in holdings_data:
            ticker = await exchange.get_ticker(h_data['symbol'])
            last_price = ticker['last'] if ticker else (h_data['last_price'] or h_data['avg_entry_price'])
            value_usdt = h_data['amount'] * last_price
            pnl_usdt = (last_price - h_data['avg_entry_price']) * h_data['amount']
            pnl_pct = (last_price / h_data['avg_entry_price'] - 1) * 100 if h_data['avg_entry_price'] else 0.0
            total_portfolio_usdt += value_usdt
            total_pnl_usdt += pnl_usdt
            assets_lines += (
                f"\n<b>{h_data['symbol'].split('/')[0]}:</b> {h_data['amount']:.6f}"
                f" (≈ {value_usdt:.2f} USDT, P&L: {pnl_usdt:+.2f} USDT / {pnl_pct:+.2f}%)"
            )
        assets_block = assets_lines if assets_lines else "Нет других активов"
        
        text = f"""
💰 <b>Баланс</b>

<b>USDT:</b>
• Свободно: {usdt.get('free', 0):.2f}
• В ордерах: {usdt.get('used', 0):.2f}
• Всего: {usdt.get('total', 0):.2f}

<b>Портфель (кроме USDT):</b> {total_portfolio_usdt:.2f} USDT
<b>P&L по активам:</b> {total_pnl_usdt:+.2f} USDT
{assets_block}
"""
        
        keyboard = [
            [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]
        ]
        
        await query.edit_message_text(
            text, 
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_balance: {e}")
        await query.answer(f"❌ Ошибка: {str(e)[:80]}", show_alert=True)


async def handle_positions_list(query):
    """Список позиций"""
    try:
        # Загружаем данные в словари внутри сессии
        with get_db() as db:
            positions = db.query(Position).filter(
                Position.status.in_([
                    PositionStatus.OPEN,
                    PositionStatus.PARTIAL_TP1,
                    PositionStatus.PARTIAL_TP2
                ])
            ).all()
            
            # Сохраняем только нужные данные в словари до закрытия сессии
            positions_data = [
                {
                    'id': p.id,
                    'symbol': p.symbol,
                    'entry_price': p.entry_price,
                }
                for p in positions
            ]
        
        if not positions_data:
            text = "📭 Нет открытых позиций"
            keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]]
            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        
        text = "📈 <b>Открытые позиции</b>\n"
        keyboard = []
        
        for pos_data in positions_data:
            current_price = pos_data['entry_price']
            if exchange:
                try:
                    ticker = await exchange.get_ticker(pos_data['symbol'])
                    if ticker:
                        current_price = ticker['last']
                except Exception as e:
                    logger.warning(f"Ошибка получения цены {pos_data['symbol']}: {e}")
            
            pnl_pct = (current_price / pos_data['entry_price'] - 1) * 100
            pnl_emoji = "🟢" if pnl_pct >= 0 else "🔴"
            
            text += f"\n{pnl_emoji} <b>{pos_data['symbol']}</b>: {pnl_pct:+.1f}%"
            
            keyboard.append([
                InlineKeyboardButton(
                    f"❌ Закрыть {pos_data['symbol'].split('/')[0]}", 
                    callback_data=f"close_{pos_data['id']}"
                )
            ])
        
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")])
        
        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Ошибка handle_positions_list: {e}")
        await query.answer("❌ Ошибка при загрузке позиций", show_alert=True)


async def handle_history(query):
    """История сделок"""
    try:
        # Загружаем данные в словари внутри сессии
        with get_db() as db:
            trades = db.query(Trade).order_by(
                Trade.created_at.desc()
            ).limit(10).all()
            
            # Сохраняем только нужные данные в списки до закрытия сессии
            trades_data = [
                {
                    'side': t.side,
                    'symbol': t.symbol,
                    'price': t.price,
                    'pnl_usdt': t.pnl_usdt,
                }
                for t in trades
            ]
        
        if not trades_data:
            text = "📭 История пуста"
        else:
            text = "📋 <b>Последние сделки</b>\n"
            
            for trade_data in trades_data:
                side_emoji = "🟢" if trade_data['side'] == "BUY" else "🔴"
                pnl_text = ""
                if trade_data['pnl_usdt'] is not None:
                    pnl_emoji = "✅" if trade_data['pnl_usdt'] >= 0 else "❌"
                    pnl_text = f" | {pnl_emoji} ${trade_data['pnl_usdt']:+.2f}"
                
                text += f"\n{side_emoji} {trade_data['symbol']} @ {trade_data['price']:.6f}{pnl_text}"
        
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]]
        
        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Ошибка handle_history: {e}")
        await query.answer("❌ Ошибка при загрузке истории", show_alert=True)


async def handle_scan_toggle(query):
    """Меню сканирования с переключателем вкл/выкл"""
    from src.database.models import BotSettings
    
    # Проверяем состояние в БД
    with get_db() as db:
        setting = db.query(BotSettings).filter(BotSettings.key == "scan_enabled").first()
        if setting:
            enabled = setting.value.lower() == "true"
        else:
            enabled = False
    
    status = "🟢 Включено" if enabled else "🔴 Выключено"
    toggle_text = "⏸️ Выключить" if enabled else "▶️ Включить"
    
    keyboard = [
        [InlineKeyboardButton(toggle_text, callback_data="scan_toggle_switch")],
        [InlineKeyboardButton("🔍 Сканировать сейчас", callback_data="force_scan")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")],
    ]
    
    text = f"""
⚙️ <b>Сканирование</b>

Статус: {status}

• Автоскан каждые 60 сек (когда включен)
• Проверка позиций каждые 30 сек

Нажми "{toggle_text}" чтобы переключить.
"""
    
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_scan_toggle_switch(query):
    """Переключение сканирования вкл/выкл"""
    from src.database.models import BotSettings
    
    with get_db() as db:
        setting = db.query(BotSettings).filter(BotSettings.key == "scan_enabled").first()
        
        if not setting:
            setting = BotSettings(key="scan_enabled", value="True")
            db.add(setting)
            new_state = True
        else:
            current = setting.value.lower() == "true"
            new_state = not current
            setting.value = str(new_state)
        
        db.commit()
    
    status_emoji = "✅ Включено" if new_state else "⏸️ Выключено"
    
    await query.answer(f"Сканирование {status_emoji}", show_alert=False)
    
    # Возвращаемся в меню сканирования
    await handle_scan_toggle(query)


async def handle_presignals_menu(query):
    """Меню управления предсигналами"""
    from src.database.models import BotSettings
    
    # Проверяем состояние в БД
    with get_db() as db:
        setting = db.query(BotSettings).filter(BotSettings.key == "presignals_enabled").first()
        if setting:
            enabled = setting.value.lower() == "true"
        else:
            enabled = PRESIGNALS["enabled"]
    
    status = "🟢 Включены" if enabled else "🔴 Отключены"
    
    # Получаем интервал из БД
    with get_db() as db:
        interval_setting = db.query(BotSettings).filter(BotSettings.key == "presignals_interval").first()
        if interval_setting:
            interval = int(interval_setting.value)
        else:
            interval = PRESIGNALS["check_interval_minutes"]
    
    keyboard = [
        [
            InlineKeyboardButton("🔔 Включить" if not enabled else "🔕 Отключить", 
                               callback_data="presignals_toggle")
        ],
        [InlineKeyboardButton("📊 Показать текущие", callback_data="presignals_list")],
        [InlineKeyboardButton("⚙️ Интервал", callback_data="presignals_interval")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")],
    ]
    
    text = f"""
🎯 <b>Предсигналы</b>

Статус: {status}
Интервал проверки: каждые {interval} мин
Минимум условий: {PRESIGNALS["min_conditions_met"]}/4

<b>Предсигнал</b> - это уведомление о паре, которая близка к полному сигналу (выполняет 2-3 условия).

Например:
✅ EMA структура
✅ RSI в импульсной зоне
❌ Рост свечи
❌ Volume spike
"""
    
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_presignals_toggle(query):
    """Переключение предсигналов вкл/выкл"""
    from src.database.models import BotSettings
    
    with get_db() as db:
        # Получаем текущее состояние из БД
        setting = db.query(BotSettings).filter(BotSettings.key == "presignals_enabled").first()
        
        if not setting:
            # Создаём запись если её нет
            current = PRESIGNALS["enabled"]
            setting = BotSettings(key="presignals_enabled", value=str(current))
            db.add(setting)
        
        # Переключаем состояние
        current = setting.value.lower() == "true"
        new_state = not current
        setting.value = str(new_state)
        db.commit()
    
    status_emoji = "✅ Включены" if new_state else "❌ Отключены"
    
    text = f"""
🎯 <b>Предсигналы {status_emoji}</b>

Настройка сохранена!
Предсигналы будут {'отправляться' if new_state else 'остановлены'} при следующей проверке.
"""
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="presignals_menu")]]
    
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_presignals_interval(query):
    """Выбор интервала проверки предсигналов"""
    from src.database.models import BotSettings
    
    # Получаем текущий интервал
    with get_db() as db:
        setting = db.query(BotSettings).filter(BotSettings.key == "presignals_interval").first()
        if setting:
            current_interval = int(setting.value)
        else:
            current_interval = PRESIGNALS["check_interval_minutes"]
    
    # Кнопки выбора интервала
    keyboard = [
        [
            InlineKeyboardButton(
                f"1 мин{'  ✅' if current_interval == 1 else ''}", 
                callback_data="presignals_interval_set_1"
            ),
            InlineKeyboardButton(
                f"2 мин{'  ✅' if current_interval == 2 else ''}", 
                callback_data="presignals_interval_set_2"
            ),
        ],
        [
            InlineKeyboardButton(
                f"5 мин{'  ✅' if current_interval == 5 else ''}", 
                callback_data="presignals_interval_set_5"
            ),
            InlineKeyboardButton(
                f"10 мин{'  ✅' if current_interval == 10 else ''}", 
                callback_data="presignals_interval_set_10"
            ),
        ],
        [
            InlineKeyboardButton(
                f"15 мин{'  ✅' if current_interval == 15 else ''}", 
                callback_data="presignals_interval_set_15"
            ),
            InlineKeyboardButton(
                f"30 мин{'  ✅' if current_interval == 30 else ''}", 
                callback_data="presignals_interval_set_30"
            ),
        ],
        [InlineKeyboardButton("🔙 Назад", callback_data="presignals_menu")],
    ]
    
    text = f"""
⏰ <b>Интервал проверки предсигналов</b>

Текущий интервал: <b>{current_interval} мин</b>

Выбери новый интервал:
"""
    
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_presignals_interval_set(query, interval_minutes: int):
    """Установка интервала проверки предсигналов"""
    from src.database.models import BotSettings
    
    with get_db() as db:
        setting = db.query(BotSettings).filter(BotSettings.key == "presignals_interval").first()
        
        if not setting:
            # Создаём новую запись
            setting = BotSettings(key="presignals_interval", value=str(interval_minutes))
            db.add(setting)
        else:
            # Обновляем существующую
            setting.value = str(interval_minutes)
        
        db.commit()
    
    text = f"""
✅ <b>Интервал сохранён!</b>

Предсигналы будут проверяться каждые <b>{interval_minutes} минут</b>

Изменение вступит в силу при следующей проверке.
"""
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="presignals_menu")]]
    
    await query.answer(f"✅ Интервал установлен: {interval_minutes} мин", show_alert=False)
    
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_force_scan(query):
    """Принудительный запуск сканирования"""
    await query.edit_message_text(
        "🔍 <b>Сканирую рынок...</b>\n\nЭто может занять 1-2 минуты.",
        parse_mode="HTML"
    )
    
    # Отправляем уведомление о начале
    if telegram_bot:
        await telegram_bot.send_message("🔍 Запущен принудительный скан рынка...")


async def handle_back_to_main(query):
    """Возврат в главное меню"""
    # Получаем статус сканирования и предсигналов из БД
    from src.database.models import BotSettings
    
    with get_db() as db:
        scan_setting = db.query(BotSettings).filter(BotSettings.key == "scan_enabled").first()
        presignals_setting = db.query(BotSettings).filter(BotSettings.key == "presignals_enabled").first()
        
        scan_enabled = scan_setting and scan_setting.value.lower() == "true"
        presignals_enabled = presignals_setting and presignals_setting.value.lower() == "true"
    
    scan_status = "🟢 Включено" if scan_enabled else "🔴 Выключено"
    presignals_status = "🟢 Включено" if presignals_enabled else "🔴 Выключено"
    
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
            InlineKeyboardButton("🎯 Предсигналы", callback_data="presignals_menu"),
        ],
    ]
    
    text = f"""
🤖 <b>Breakout Scanner Bot</b>

<b>Статус:</b>
⚙️ Сканирование: {scan_status}
🎯 Предсигналы: {presignals_status}

Выбери действие:
"""
    
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_custom_amount_message(update: Update, context):
    """Обработка текстового сообщения с кастомной суммой"""
    user_id = update.effective_user.id
    message_text = update.message.text.strip()
    
    # Проверяем, ожидается ли от пользователя ввод суммы
    if user_id not in pending_custom_amounts:
        return  # Игнорируем сообщение, если не ожидаем ввода
    
    signal_id = pending_custom_amounts[user_id]
    
    # Валидация ввода
    try:
        amount_usdt = float(message_text)
        
        if amount_usdt <= 0:
            await update.message.reply_text(
                "❌ Сумма должна быть больше 0\n\nПопробуйте ещё раз:",
                parse_mode="HTML"
            )
            return
        
        if amount_usdt > 10000:
            await update.message.reply_text(
                "❌ Сумма не может превышать $10,000\n\nПопробуйте ещё раз:",
                parse_mode="HTML"
            )
            return
            
    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат\n\nВведите число (например: 15 или 75.5):",
            parse_mode="HTML"
        )
        return
    
    # Удаляем из очереди ожидания
    del pending_custom_amounts[user_id]
    
    # Выполняем покупку
    await update.message.reply_text(
        f"⏳ Выполняю покупку на сумму <b>${amount_usdt:.2f}</b>...",
        parse_mode="HTML"
    )
    
    from src.database.models import Signal, SignalStatus
    
    with get_db() as db:
        signal = db.query(Signal).filter(Signal.id == signal_id).first()
        
        if not signal:
            await update.message.reply_text("❌ Сигнал не найден")
            return
        
        if signal.status != SignalStatus.PENDING:
            await update.message.reply_text(f"⚠️ Сигнал уже обработан: {signal.status.value}")
            return
        
        symbol = signal.symbol
    
    # Выполняем покупку
    from src.trading.executor import executor
    
    if executor:
        success = await executor.execute_buy_from_signal(
            signal_id=signal_id,
            amount_usdt_override=amount_usdt
        )
        
        if success:
            await update.message.reply_text(
                f"✅ Покупка {symbol} на <b>${amount_usdt:.2f}</b> успешно выполнена!",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                f"❌ Не удалось выполнить покупку {symbol}",
                parse_mode="HTML"
            )
    else:
        await update.message.reply_text("❌ Executor не инициализирован")


def setup_handlers(app: Application):
    """Регистрация всех обработчиков"""
    from telegram.ext import MessageHandler, filters
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    # Обработчик текстовых сообщений для кастомной суммы (должен быть после команд)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_amount_message))
    
    logger.info("✅ Обработчики Telegram зарегистрированы")
