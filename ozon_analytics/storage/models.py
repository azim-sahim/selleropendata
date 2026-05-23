"""
SQLAlchemy модели данных для хранения информации из Ozon API.

Модели включают:
- Товары (Products)
- Заказы (Orders)
- Финансовые операции (FinanceOperations)
- Рекламные кампании (AdCampaigns)
- Статистика рекламы (AdStats)
- Unit-экономика по SKU (UnitEconomics)
"""

from datetime import datetime, date
from decimal import Decimal
from typing import Optional, List
from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    DateTime,
    Date,
    Boolean,
    Text,
    Numeric,
    ForeignKey,
    Index,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship, declarative_base, Mapped, mapped_column
from sqlalchemy.sql import func

Base = declarative_base()


class Product(Base):
    """
    Модель товара Ozon.
    
    Хранит информацию о товарах из каталога продавца.
    """
    __tablename__ = "products"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ozon_product_id: Mapped[int] = mapped_column(Integer, unique=True, index=True, nullable=False)
    sku: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    brand: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    
    # Цены
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cost_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Закупочная цена
    old_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    # Остатки
    stock_qty: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reserved_qty: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    
    # Характеристики
    weight: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Вес в кг
    dimensions: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # "ДxШxВ"
    
    # Статус
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    
    # Метаданные
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    
    # Связи
    orders: Mapped[List["Order"]] = relationship("Order", back_populates="product", lazy="select")
    unit_economics: Mapped[List["UnitEconomics"]] = relationship("UnitEconomics", back_populates="product", lazy="select")
    
    __table_args__ = (
        Index("idx_product_category", "category"),
        Index("idx_product_status", "status"),
    )
    
    def __repr__(self) -> str:
        return f"<Product(id={self.ozon_product_id}, name='{self.name}', sku={self.sku})>"


class Order(Base):
    """
    Модель заказа.
    
    Хранит информацию о заказах (FBS/FBO).
    """
    __tablename__ = "orders"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    posting_number: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    order_number: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    
    # Статусы
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    delivery_method: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    
    # Даты
    order_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    delivery_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    cancel_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    
    # Товары в заказе (JSON строка для простоты, можно нормализовать)
    items_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    # Финансы
    total_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    commission_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    logistics_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ecwiring_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    # Связь с товаром (основной товар в заказе)
    product_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("products.ozon_product_id"), nullable=True)
    product: Mapped[Optional["Product"]] = relationship("Product", back_populates="orders")
    
    # Клиент
    customer_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    region: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    
    # Метаданные
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    
    __table_args__ = (
        Index("idx_order_date", "order_date"),
        Index("idx_order_status", "status"),
    )
    
    def __repr__(self) -> str:
        return f"<Order(posting_number='{self.posting_number}', status='{self.status}')>"


class FinanceOperation(Base):
    """
    Модель финансовой операции.
    
    Хранит данные из отчета о движении денежных средств.
    """
    __tablename__ = "finance_operations"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    operation_type: Mapped[str] = mapped_column(String(100), nullable=False)
    
    # Дата и период
    operation_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    period_start: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    period_end: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    
    # Суммы
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    balance_before: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    balance_after: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    # Детали
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    posting_number: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, index=True)
    product_sku: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    
    # Категория операции
    category: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    subcategory: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    
    # Метаданные
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    
    __table_args__ = (
        Index("idx_finance_type", "operation_type"),
        Index("idx_finance_category", "category"),
    )
    
    def __repr__(self) -> str:
        return f"<FinanceOperation(id='{self.operation_id}', type='{self.operation_type}', amount={self.amount})>"


class AdCampaign(Base):
    """
    Модель рекламной кампании.
    
    Хранит информацию о рекламных кампаниях Ozon.
    """
    __tablename__ = "ad_campaigns"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(Integer, unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    
    # Тип и статус
    campaign_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="active")
    
    # Бюджет
    budget: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    daily_budget: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    # Ставки
    default_bid: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    # Даты
    start_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    
    # Привязка к товарам (SKU через JSON)
    target_skus_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    # Метаданные
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    
    # Связи
    stats: Mapped[List["AdStats"]] = relationship("AdStats", back_populates="campaign", lazy="select")
    
    __table_args__ = (
        Index("idx_campaign_status", "status"),
        Index("idx_campaign_type", "campaign_type"),
    )
    
    def __repr__(self) -> str:
        return f"<AdCampaign(id={self.campaign_id}, name='{self.name}', status='{self.status}')>"


class AdStats(Base):
    """
    Модель статистики рекламной кампании/объявления.
    
    Хранит ежедневную статистику по кампаниям.
    """
    __tablename__ = "ad_stats"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    
    # Связь с кампанией
    campaign_id: Mapped[int] = mapped_column(Integer, ForeignKey("ad_campaigns.campaign_id"), nullable=False, index=True)
    campaign: Mapped["AdCampaign"] = relationship("AdCampaign", back_populates="stats")
    
    # ID объявления (опционально)
    ads_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    
    # Дата
    stat_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    
    # Показы и клики
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    
    # Расходы
    spend: Mapped[float] = mapped_column(Float, default=0.0)
    
    # Конверсии
    orders: Mapped[int] = mapped_column(Integer, default=0)
    revenue: Mapped[float] = mapped_column(Float, default=0.0)
    
    # Вычисляемые метрики (кэшируются для производительности)
    ctr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # CTR %
    cpc: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # CPC
    acos: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # ACOS %
    roas: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # ROAS
    cpa: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # CPA
    
    # Метаданные
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    
    __table_args__ = (
        UniqueConstraint("campaign_id", "ads_id", "stat_date", name="uq_campaign_ads_date"),
        Index("idx_adstats_spend", "spend"),
        Index("idx_adstats_roas", "roas"),
    )
    
    def __repr__(self) -> str:
        return f"<AdStats(campaign_id={self.campaign_id}, date={self.stat_date}, spend={self.spend})>"


class UnitEconomics(Base):
    """
    Модель unit-экономики по SKU.
    
    Хранит рассчитанные метрики прибыльности для каждого товара.
    """
    __tablename__ = "unit_economics"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    
    # Связь с товаром
    product_id: Mapped[int] = mapped_column(Integer, ForeignKey("products.ozon_product_id"), nullable=False, index=True)
    product: Mapped["Product"] = relationship("Product", back_populates="unit_economics")
    sku: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    
    # Период расчета
    period_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    period_end: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    
    # Объемы
    quantity_sold: Mapped[int] = mapped_column(Integer, default=0)
    
    # Выручка
    revenue: Mapped[float] = mapped_column(Float, default=0.0)
    
    # Прямые затраты
    cogs: Mapped[float] = mapped_column(Float, default=0.0)  # Себестоимость (Cost of Goods Sold)
    
    # Комиссии и сборы Ozon
    ozon_commission: Mapped[float] = mapped_column(Float, default=0.0)
    logistics_cost: Mapped[float] = mapped_column(Float, default=0.0)
    ecwiring_cost: Mapped[float] = mapped_column(Float, default=0.0)  # Эквайринг
    
    # Реклама
    ad_spend: Mapped[float] = mapped_column(Float, default=0.0)
    
    # Налоги
    tax_amount: Mapped[float] = mapped_column(Float, default=0.0)
    
    # Итоговые метрики
    gross_profit: Mapped[float] = mapped_column(Float, default=0.0)  # Выручка - Себестоимость
    net_profit: Mapped[float] = mapped_column(Float, default=0.0)  # Чистая прибыль
    
    # Маржинальность
    margin_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # % маржи
    markup_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # % наценки
    
    # Юнит-метрики (на 1 единицу товара)
    unit_revenue: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    unit_cost: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    unit_profit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    # Точки безубыточности
    break_even_acos: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Максимальный ACOS для безубыточности
    
    # Рекомендации
    recommendation: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    is_profitable: Mapped[bool] = mapped_column(Boolean, default=True)
    
    # Метаданные
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    calculated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    
    __table_args__ = (
        Index("idx_ue_period", "period_start", "period_end"),
        Index("idx_ue_profitability", "is_profitable"),
    )
    
    def __repr__(self) -> str:
        return f"<UnitEconomics(sku={self.sku}, period={self.period_start}, profit={self.net_profit})>"


class SyncLog(Base):
    """
    Лог синхронизации данных.
    
    Отслеживает последнюю успешную синхронизацию для каждого типа данных.
    """
    __tablename__ = "sync_logs"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sync_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)  # products, orders, finance, ads
    
    # Временные метки
    last_sync_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_successful_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)  # Последняя дата данных
    
    # Статистика
    records_loaded: Mapped[int] = mapped_column(Integer, default=0)
    records_updated: Mapped[int] = mapped_column(Integer, default=0)
    records_failed: Mapped[int] = mapped_column(Integer, default=0)
    
    # Статус
    status: Mapped[str] = mapped_column(String(20), default="success")  # success, partial, failed
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    # Метаданные
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    
    __table_args__ = (
        UniqueConstraint("sync_type", name="uq_sync_type"),
    )
    
    def __repr__(self) -> str:
        return f"<SyncLog(type='{self.sync_type}', status='{self.status}', loaded={self.records_loaded})>"
