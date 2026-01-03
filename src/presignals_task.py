"""
Фоновый таск для проверки и отправки предсигналов
"""
import asyncio
from datetime import datetime, timedelta
from loguru import logger

from pybit.unified_trading import HTTP
from src.config import SIGNAL_CONDITIONS, INDICATORS, PRESIGNALS, TELEGRAM_CHAT_ID
from src.exchange.indicators import calculate_indicators, detect_presignals
from src.telegram.presignals import send_presignal_notification

async def presignals_monitor_task(bot, exchange):
    """
    Фоновый таск для мониторинга предсигналов.
    
    Запускается с интервалом из конфига (обычно 5 мин).
    Проверяет все пары и отправляет уведомления о близких к сигналу.
    """
    from src.database import get_db
    from src.database.models import BotSettings
    
    # Проверяем, включены ли предсигналы в БД (приоритет перед конфигом)
    with get_db() as db:
        setting = db.query(BotSettings).filter(BotSettings.key == "presignals_enabled").first()
        if setting:
            enabled = setting.value.lower() == "true"
        else:
            enabled = PRESIGNALS["enabled"]
    
    if not enabled:
        logger.info("🎯 Предсигналы отключены")
        return
    
    logger.info("🔍 Запуск проверки предсигналов...")
    
    try:
        # aiogram Application хранит реальный Bot в атрибуте .bot у нашего TelegramBot
        tg_bot = bot.bot if hasattr(bot, "bot") else bot
        session = HTTP(testnet=False)
        tickers = session.get_tickers(category="spot")
        
        usdt_pairs = [t for t in tickers["result"]["list"] if t["symbol"].endswith("USDT")]
        
        presignals_found = []
        checked_count = 0
        
        logger.info(f"📊 Проверяем {len(usdt_pairs)} пар на предсигналы...")
        
        for t in usdt_pairs:
            symbol = t["symbol"]
            checked_count += 1
            
            try:
                # Получаем исторические данные
                candles = session.get_kline(
                    category="spot",
                    symbol=symbol,
                    interval="5",
                    limit=120
                )
                
                if not candles["result"]["list"]:
                    continue
                
                ohlcv = [[float(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])] 
                         for c in reversed(candles["result"]["list"])]
                
                df = calculate_indicators(ohlcv)
                if df is None or len(df) < 100:
                    continue
                
                # Проверяем предсигнал
                presignal_data = detect_presignals(df)
                conditions_met = presignal_data.get("conditions_met", 0)
                
                # Если выполнено минимум условий - добавляем в список
                if conditions_met >= PRESIGNALS["min_conditions_met"]:
                    presignals_found.append({
                        'symbol': symbol,
                        'conditions_met': conditions_met,
                        'price': df.iloc[-1]['close'],
                        'data': presignal_data,
                    })
                
                # Прогресс
                if checked_count % 50 == 0:
                    logger.info(f"  Проверено {checked_count}/{len(usdt_pairs)} пар...")
            
            except Exception as e:
                logger.debug(f"  Ошибка при проверке {symbol}: {e}")
                continue
        
        logger.info(f"✅ Проверена {checked_count} пар")
        
        if presignals_found:
            logger.info(f"🎯 Найдено {len(presignals_found)} предсигналов!")
            
            # Сортируем по количеству выполненных условий
            presignals_found.sort(key=lambda x: x['conditions_met'], reverse=True)
            
            # Отправляем уведомления о топ предсигналах (не более 5 за раз)
            for presignal in presignals_found[:5]:
                try:
                    await send_presignal_notification(
                        bot=tg_bot,
                        chat_id=TELEGRAM_CHAT_ID,
                        symbol=presignal['symbol'],
                        presignal_data=presignal['data'],
                        price=presignal['price']
                    )
                    
                    # Небольшая задержка между сообщениями
                    await asyncio.sleep(0.5)
                
                except Exception as e:
                    logger.error(f"Ошибка отправки уведомления о {presignal['symbol']}: {e}")
        
        else:
            logger.info("😴 Предсигналов не найдено")
    
    except Exception as e:
        logger.error(f"Ошибка в таске предсигналов: {e}")


async def start_presignals_task(bot, exchange):
    """
    Запускает фоновый таск для мониторинга предсигналов.
    
    Args:
        bot: Telegram bot instance
        exchange: BybitExchange instance
    """
    from src.database import get_db
    from src.database.models import BotSettings
    
    logger.info(f"🎯 Таск предсигналов запущен (интервал: {PRESIGNALS['check_interval_minutes']} мин по умолчанию)")
    
    # При запуске выключаем предсигналы по умолчанию
    try:
        with get_db() as db:
            setting = db.query(BotSettings).filter(BotSettings.key == "presignals_enabled").first()
            if not setting:
                setting = BotSettings(key="presignals_enabled", value="False")
                db.add(setting)
            else:
                setting.value = "False"
            db.commit()
        logger.info("⏸️ Предсигналы выключены по умолчанию. Включите через меню.")
    except Exception as e:
        logger.error(f"❌ Ошибка установки presignals_enabled: {e}")
    
    while True:
        try:
            # Проверяем включены ли предсигналы перед запуском
            with get_db() as db:
                setting = db.query(BotSettings).filter(BotSettings.key == "presignals_enabled").first()
                enabled = setting and setting.value.lower() == "true"
            
            if enabled:
                await presignals_monitor_task(bot, exchange)
            # Если выключены - просто пропускаем проверку
        except Exception as e:
            logger.error(f"Критическая ошибка в таске предсигналов: {e}")
        
        # Получаем интервал из БД (или используем конфиг)
        with get_db() as db:
            setting = db.query(BotSettings).filter(BotSettings.key == "presignals_interval").first()
            if setting:
                interval_minutes = int(setting.value)
            else:
                interval_minutes = PRESIGNALS["check_interval_minutes"]
        
        interval_seconds = interval_minutes * 60
        
        # Ждём перед следующей проверкой
        await asyncio.sleep(interval_seconds)
