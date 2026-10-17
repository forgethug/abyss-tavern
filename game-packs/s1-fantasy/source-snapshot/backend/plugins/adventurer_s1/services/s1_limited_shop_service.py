"""限时摊位的持久化商品清单与时间窗口；活动内容由管理员配置。"""
from datetime import datetime, timezone


LIMITED_SHOP_HANDLER = 'limited_shop'


def limited_shop_catalog(template):
    """返回经校验的配置；缺失或损坏时关闭购买。时间必须带时区。"""
    config = template.settlement_config or {}
    try:
        start = datetime.fromisoformat(config['sale_starts_at'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(config['sale_ends_at'].replace('Z', '+00:00'))
        prices = config['sale_prices']
        if (start.tzinfo is None or end.tzinfo is None or start >= end
                or not isinstance(prices, dict) or not prices
                or any(not isinstance(name, str) or not name.strip()
                       or type(price) is not int or price <= 0
                       for name, price in prices.items())):
            raise ValueError('invalid sale configuration')
        return start, end, prices
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def limited_shop_rejection(template, now=None):
    if not getattr(template, 'is_active', True):
        return '摊位已暂停开放'
    config = template.settlement_config or {}
    if not isinstance(config, dict) or config.get('sale_enabled', True) is not True:
        return '摊位已暂停开放'
    catalog = limited_shop_catalog(template)
    if catalog is None:
        return '摊位暂未开放'
    start, end, _ = catalog
    now = now or datetime.now(timezone.utc)
    if now < start:
        return '摊位尚未开张'
    if now >= end:
        return '本次限时售卖已结束，愿与你下次月圆再会'
    return None
