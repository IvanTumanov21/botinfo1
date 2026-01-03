"""
Проверка количества пар на Bybit и фильтрации
"""
from pybit.unified_trading import HTTP
from src.config import ASSET_FILTERS

session = HTTP(testnet=False)
tickers = session.get_tickers(category="spot")

usdt_pairs = [t for t in tickers["result"]["list"] if t["symbol"].endswith("USDT")]
print(f"📊 Всего USDT пар на Bybit: {len(usdt_pairs)}")

# После фильтров
filtered = []
excluded_by_price = 0
excluded_by_volume = 0
excluded_by_base = 0

for t in usdt_pairs:
    try:
        price = float(t["lastPrice"])
        volume = float(t["turnover24h"])
        base = t["symbol"].replace("USDT", "")
        
        if price < ASSET_FILTERS["min_price"] or price > ASSET_FILTERS["max_price"]:
            excluded_by_price += 1
            continue
        if volume < ASSET_FILTERS["min_volume_24h"]:
            excluded_by_volume += 1
            continue
        if base in ASSET_FILTERS["excluded_bases"]:
            excluded_by_base += 1
            continue
        
        filtered.append(t["symbol"])
    except:
        pass

print(f"✅ После фильтров: {len(filtered)} пар")
print(f"\n❌ Исключено:")
print(f"  По цене (не {ASSET_FILTERS['min_price']}-{ASSET_FILTERS['max_price']} USDT): {excluded_by_price}")
print(f"  По объёму (<{ASSET_FILTERS['min_volume_24h']:,} за 24ч): {excluded_by_volume}")
print(f"  По базе (BTC/ETH/стейблы): {excluded_by_base}")
print(f"\n🔍 Активные фильтры:")
print(f"  • Цена: {ASSET_FILTERS['min_price']} - {ASSET_FILTERS['max_price']} USDT")
print(f"  • Объём 24ч: ≥{ASSET_FILTERS['min_volume_24h']:,} USDT")
print(f"  • Исключено баз: {', '.join(ASSET_FILTERS['excluded_bases'])}")
