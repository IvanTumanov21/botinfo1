"""
🚀 Bybit Breakout Scanner Bot
Главный файл запуска
"""
import asyncio
import signal
import sys
from datetime import datetime, timezone
from loguru import logger

# Настройка логирования
logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    level="INFO"
)
logger.add(
    "logs/bot_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="7 days",
    level="DEBUG"
)

from src.config import SCAN_INTERVALS, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
from src.database.connection import init_db
from src.exchange import BybitExchange, MarketScanner
from src.telegram import TelegramBot, setup_handlers
from src.telegram.handlers import set_components
from src.trading import OrderExecutor, PositionManager

from telegram.ext import Application


# Глобальные компоненты
exchange: BybitExchange = None
scanner: MarketScanner = None
telegram_bot: TelegramBot = None
executor: OrderExecutor = None
position_manager: PositionManager = None
app: Application = None

running = True


async def scan_loop():
    """Цикл сканирования рынка"""
    global running
    
    logger.info("🔍 Запуск цикла сканирования...")
    await asyncio.sleep(5)  # Даём время на инициализацию
    
    while running:
        try:
            logger.info("🔄 Начинаем сканирование...")
            # Сканируем рынок
            signals = await scanner.scan_all()
            
            # Отправляем найденные сигналы
            for sig in signals:
                signal_id = await scanner.save_signal_to_db(sig)
                await telegram_bot.send_signal(sig, signal_id)
                
                # Небольшая пауза между сигналами
                await asyncio.sleep(2)
            
            # Ждём следующего сканирования
            await asyncio.sleep(SCAN_INTERVALS["signal_scan_sec"])
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Ошибка в цикле сканирования: {e}")
            await asyncio.sleep(30)


async def position_loop():
    """Цикл проверки позиций"""
    global running
    
    logger.info("📊 Запуск цикла проверки позиций...")
    
    while running:
        try:
            # Проверяем позиции
            actions = await position_manager.check_all_positions()
            
            # Отправляем уведомления о действиях
            for action in actions:
                await telegram_bot.send_trade_executed(action)
            
            # Ждём следующей проверки
            await asyncio.sleep(SCAN_INTERVALS["position_check_sec"])
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Ошибка в цикле позиций: {e}")
            await asyncio.sleep(10)


def signal_handler(sig, frame):
    """Обработчик сигналов завершения"""
    global running
    logger.info("⏹ Получен сигнал завершения...")
    running = False


async def main():
    """Главная функция"""
    global exchange, scanner, telegram_bot, executor, position_manager, app, running
    
    # Регистрируем обработчики сигналов
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info("=" * 50)
    logger.info("🚀 Запуск Bybit Breakout Scanner Bot")
    logger.info("=" * 50)
    
    # 1. Инициализация БД
    logger.info("📦 Инициализация базы данных...")
    init_db()
    
    # 2. Подключение к бирже
    logger.info("🔗 Подключение к Bybit...")
    exchange = BybitExchange()
    connected = await exchange.connect()
    
    if not connected:
        logger.error("❌ Не удалось подключиться к Bybit")
        return
    
    # 3. Инициализация компонентов
    scanner = MarketScanner(exchange)
    executor = OrderExecutor(exchange)
    position_manager = PositionManager(exchange, executor)
    
    # 4. Инициализация Telegram
    logger.info("📱 Инициализация Telegram...")
    telegram_bot = TelegramBot()
    await telegram_bot.init()
    
    # 5. Настройка Telegram Application
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    setup_handlers(app)
    set_components(exchange, telegram_bot)
    
    # 6. Инициализация Telegram
    await app.initialize()
    await app.start()
    
    # Запускаем polling как отдельный таск
    polling_task = asyncio.create_task(
        app.updater.start_polling(drop_pending_updates=True)
    )
    
    # Даём время на запуск polling
    await asyncio.sleep(1)
    
    # 7. Отправляем стартовое сообщение
    await telegram_bot.send_message(
        "✅ <b>Бот запущен!</b>\n\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n"
        "Используй /start для управления"
    )
    
    logger.info("✅ Бот полностью запущен!")
    logger.info("=" * 50)
    
    # 8. Запускаем рабочие циклы
    logger.info("🔄 Запуск фоновых тасков...")
    scan_task = asyncio.create_task(scan_loop())
    position_task = asyncio.create_task(position_loop())
    logger.info("✅ Таски созданы, ожидаем сигнала завершения...")
    
    # 9. Ждём завершения (SIGTERM/SIGINT)
    try:
        while running:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    
    # 10. Graceful shutdown
    logger.info("⏹ Остановка бота...")
    
    scan_task.cancel()
    position_task.cancel()
    
    try:
        await scan_task
    except asyncio.CancelledError:
        pass
    
    try:
        await position_task
    except asyncio.CancelledError:
        pass
    
    # Останавливаем Telegram
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    
    # Закрываем биржу
    await exchange.close()
    
    # Финальное сообщение
    logger.info("👋 Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Завершение по Ctrl+C")
