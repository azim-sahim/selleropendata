"""
Модуль работы с базой данных SQLAlchemy.

Функции:
- Инициализация БД и создание таблиц
- Управление сессиями
- CRUD операции для основных моделей
"""

import logging
from typing import Optional, List, Type, TypeVar, AsyncGenerator
from datetime import datetime, date
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncSession,
    async_sessionmaker,
    AsyncEngine,
    AsyncAttrs,
)
from sqlalchemy import select, update, delete, func, text
from sqlalchemy.exc import SQLAlchemyError

from config import config
from storage.models import Base, SyncLog, Product, Order, FinanceOperation, AdCampaign, AdStats, UnitEconomics

logger = logging.getLogger(__name__)

T = TypeVar('T', bound=Base)


class DatabaseManager:
    """
    Менеджер базы данных для управления подключениями и сессиями.
    
    Поддерживает асинхронные операции через SQLAlchemy 2.0+.
    """
    
    def __init__(self, database_url: Optional[str] = None):
        self.database_url = database_url or config.database_url
        self._engine: Optional[AsyncEngine] = None
        self._session_maker: Optional[async_sessionmaker[AsyncSession]] = None
    
    @property
    def engine(self) -> AsyncEngine:
        """Получить или создать движок БД."""
        if self._engine is None:
            # Настройка движка в зависимости от типа БД
            if "sqlite" in self.database_url:
                self._engine = create_async_engine(
                    self.database_url,
                    echo=config.debug,
                    connect_args={"check_same_thread": False},
                )
            else:
                # PostgreSQL и другие БД
                self._engine = create_async_engine(
                    self.database_url,
                    echo=config.debug,
                    pool_size=10,
                    max_overflow=20,
                    pool_pre_ping=True,
                )
        return self._engine
    
    @property
    def session_maker(self) -> async_sessionmaker[AsyncSession]:
        """Получить или создать фабрику сессий."""
        if self._session_maker is None:
            self._session_maker = async_sessionmaker(
                bind=self.engine,
                class_=AsyncSession,
                expire_on_commit=False,
                autocommit=False,
                autoflush=False,
            )
        return self._session_maker
    
    async def init_db(self) -> None:
        """
        Инициализировать базу данных: создать все таблицы.
        
        Вызывать один раз при первом запуске приложения.
        """
        try:
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info("База данных успешно инициализирована")
        except Exception as e:
            logger.error(f"Ошибка инициализации БД: {e}")
            raise
    
    async def drop_db(self) -> None:
        """Удалить все таблицы (для тестов/сброса)."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        logger.info("Все таблицы удалены")
    
    @asynccontextmanager
    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """
        Получить сессию БД в контекстном менеджере.
        
        Usage:
            async with db_manager.get_session() as session:
                # работа с session
        """
        session = self.session_maker()
        try:
            yield session
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.error(f"Ошибка транзакции БД: {e}", exc_info=True)
            raise
        finally:
            await session.close()
    
    async def close(self) -> None:
        """Закрыть соединение с БД."""
        if self._engine:
            await self._engine.dispose()
            logger.info("Соединение с БД закрыто")


# Глобальный экземпляр
db_manager = DatabaseManager()


# === CRUD Helper Functions ===

async def get_or_create(
    session: AsyncSession,
    model: Type[T],
    defaults: Optional[dict] = None,
    **kwargs,
) -> tuple[T, bool]:
    """
    Получить существующую запись или создать новую.
    
    Args:
        session: Сессия БД
        model: Модель SQLAlchemy
        defaults: Значения по умолчанию для создания
        **kwargs: Параметры для поиска
        
    Returns:
        Кортеж (объект, was_created)
    """
    query = select(model).filter_by(**kwargs)
    result = await session.execute(query)
    instance = result.scalar_one_or_none()
    
    if instance:
        return instance, False
    
    # Создаем новый объект
    params = {**kwargs}
    if defaults:
        params.update(defaults)
    instance = model(**params)
    session.add(instance)
    
    return instance, True


async def bulk_upsert(
    session: AsyncSession,
    model: Type[T],
    records: List[dict],
    index_elements: List[str],
) -> tuple[int, int]:
    """
    Массовая вставка или обновление записей (upsert).
    
    Args:
        session: Сессия БД
        model: Модель SQLAlchemy
        records: Список словарей с данными
        index_elements: Поля для уникального индекса
        
    Returns:
        Кортеж (вставлено, обновлено)
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    
    inserted = 0
    updated = 0
    
    if not records:
        return 0, 0
    
    # Определяем тип БД
    is_sqlite = "sqlite" in config.database_url
    
    for record in records:
        if is_sqlite:
            stmt = sqlite_insert(model).values(record)
            stmt = stmt.on_conflict_do_update(
                index_elements=index_elements,
                set_={k: getattr(stmt.excluded, k) for k in record.keys() if k not in index_elements}
            )
        else:
            stmt = pg_insert(model).values(record)
            stmt = stmt.on_conflict_do_update(
                index_elements=index_elements,
                set_={k: getattr(stmt.excluded, k) for k in record.keys() if k not in index_elements}
            )
        
        await session.execute(stmt)
        updated += 1  # Для простоты считаем все как обновления
    
    return len(records), 0


async def save_sync_log(
    session: AsyncSession,
    sync_type: str,
    status: str,
    records_loaded: int = 0,
    records_updated: int = 0,
    records_failed: int = 0,
    error_message: Optional[str] = None,
    last_successful_date: Optional[date] = None,
) -> SyncLog:
    """
    Сохранить лог синхронизации.
    
    Args:
        session: Сессия БД
        sync_type: Тип синхронизации (products, orders, finance, ads)
        status: Статус (success, partial, failed)
        records_loaded: Количество загруженных записей
        records_updated: Количество обновленных записей
        records_failed: Количество неудачных записей
        error_message: Сообщение об ошибке
        last_successful_date: Последняя успешная дата данных
        
    Returns:
        Объект SyncLog
    """
    sync_log, created = await get_or_create(
        session,
        SyncLog,
        defaults={
            "last_sync_at": datetime.now(),
            "status": status,
            "records_loaded": records_loaded,
            "records_updated": records_updated,
            "records_failed": records_failed,
            "error_message": error_message,
            "last_successful_date": last_successful_date,
        },
        sync_type=sync_type,
    )
    
    if not created:
        # Обновляем существующую запись
        sync_log.last_sync_at = datetime.now()
        sync_log.status = status
        sync_log.records_loaded = records_loaded
        sync_log.records_updated = records_updated
        sync_log.records_failed = records_failed
        sync_log.error_message = error_message
        sync_log.last_successful_date = last_successful_date
    
    return sync_log


async def get_last_sync_date(session: AsyncSession, sync_type: str) -> Optional[date]:
    """
    Получить дату последней успешной синхронизации.
    
    Args:
        session: Сессия БД
        sync_type: Тип синхронизации
        
    Returns:
        Дата или None если синхронизация еще не проводилась
    """
    query = select(SyncLog).where(SyncLog.sync_type == sync_type)
    result = await session.execute(query)
    sync_log = result.scalar_one_or_none()
    
    if sync_log and sync_log.status == "success":
        return sync_log.last_successful_date
    
    return None


# === Product Operations ===

async def save_products(session: AsyncSession, products_data: List[dict]) -> int:
    """
    Сохранить список товаров в БД.
    
    Args:
        session: Сессия БД
        products_data: Список данных о товарах из API
        
    Returns:
        Количество сохраненных товаров
    """
    saved_count = 0
    
    for product_data in products_data:
        try:
            product, created = await get_or_create(
                session,
                Product,
                defaults={
                    "name": product_data.get("name", ""),
                    "sku": product_data.get("sku"),
                    "category": product_data.get("category_name"),
                    "price": product_data.get("price"),
                    "stock_qty": product_data.get("stocks", [{}])[0].get("present", 0) if product_data.get("stocks") else 0,
                    "is_active": product_data.get("is_active", True),
                    "synced_at": datetime.now(),
                },
                ozon_product_id=product_data.get("id"),
            )
            
            if not created:
                # Обновляем существующий товар
                product.name = product_data.get("name", product.name)
                product.price = product_data.get("price", product.price)
                product.stock_qty = product_data.get("stocks", [{}])[0].get("present", 0) if product_data.get("stocks") else product.stock_qty
                product.synced_at = datetime.now()
            
            saved_count += 1
            
        except Exception as e:
            logger.error(f"Ошибка сохранения товара {product_data.get('id')}: {e}")
    
    return saved_count


# === Order Operations ===

async def save_orders(session: AsyncSession, orders_data: List[dict]) -> int:
    """
    Сохранить список заказов в БД.
    
    Args:
        session: Сессия БД
        orders_data: Список данных о заказах из API
        
    Returns:
        Количество сохраненных заказов
    """
    saved_count = 0
    
        # Извлекаем финансовые данные из заказа (если есть)
        financials = order_data.get("financials", {})
    for order_data in orders_data:
        try:
            order, created = await get_or_create(
                session,
                Order,
                defaults={
                    "status": order_data.get("status", ""),
                    "delivery_method": order_data.get("delivery_method", {}).get("name"),
                    "order_date": datetime.fromisoformat(order_data.get("created_at", "").replace("Z", "+00:00")),
                    "total_amount": order_data.get("financials", {}).get("total_price"),
                    "items_json": str(order_data.get("products", [])),
                    "synced_at": datetime.now(),
                },
                posting_number=order_data.get("posting_number"),
            )
            
            if not created:
                order.status = order_data.get("status", order.status)
                order.synced_at = datetime.now()
            
            saved_count += 1
            
        except Exception as e:
            logger.error(f"Ошибка сохранения заказа {order_data.get('posting_number')}: {e}")
    
    return saved_count


# === Finance Operations ===

async def save_finance_operations(session: AsyncSession, operations_data: List[dict]) -> int:
    """
    Сохранить финансовые операции в БД.
    
    Args:
        session: Сессия БД
        operations_data: Список финансовых операций из API
        
    Returns:
        Количество сохраненных операций
    """
    saved_count = 0
    
    for op_data in operations_data:
        try:
            op, created = await get_or_create(
                session,
                FinanceOperation,
                defaults={
                    "operation_type": op_data.get("operation_type_name", ""),
                    "operation_date": datetime.strptime(op_data.get("date", ""), "%Y-%m-%d").date(),
                    "amount": op_data.get("sum", 0.0),
                    "balance_before": op_data.get("balance_before"),
                    "balance_after": op_data.get("balance_after"),
                    "description": op_data.get("description"),
                    "posting_number": op_data.get("posting_number"),
                    "category": op_data.get("operation_type_name"),
                    "synced_at": datetime.now(),
                },
                operation_id=str(op_data.get("id")),
            )
            
            saved_count += 1
            
        except Exception as e:
            logger.error(f"Ошибка сохранения финансовой операции {op_data.get('id')}: {e}")
    
    return saved_count


# === Ad Campaign Operations ===

async def save_ad_campaigns(session: AsyncSession, campaigns_data: List[dict]) -> int:
    """
    Сохранить рекламные кампании в БД.
    
    Args:
        session: Сессия БД
        campaigns_data: Список кампаний из API
        
    Returns:
        Количество сохраненных кампаний
    """
    saved_count = 0
    
    for campaign_data in campaigns_data:
        try:
            campaign, created = await get_or_create(
                session,
                AdCampaign,
                defaults={
                    "name": campaign_data.get("name", ""),
                    "campaign_type": campaign_data.get("type"),
                    "status": campaign_data.get("status", "active"),
                    "budget": campaign_data.get("budget"),
                    "daily_budget": campaign_data.get("daily_budget"),
                    "synced_at": datetime.now(),
                },
                campaign_id=campaign_data.get("id"),
            )
            
            if not created:
                campaign.status = campaign_data.get("status", campaign.status)
                campaign.synced_at = datetime.now()
            
            saved_count += 1
            
        except Exception as e:
            logger.error(f"Ошибка сохранения кампании {campaign_data.get('id')}: {e}")
    
    return saved_count


# === Ad Stats Operations ===

async def save_ad_stats(session: AsyncSession, stats_data: List[dict]) -> int:
    """
    Сохранить статистику рекламы в БД.
    
    Args:
        session: Сессия БД
        stats_data: Список статистики из API
        
    Returns:
        Количество сохраненных записей статистики
    """
    saved_count = 0
    
    for stat in stats_data:
        try:
            # Рассчитываем метрики
            impressions = stat.get("impressions", 0)
            clicks = stat.get("clicks", 0)
            spend = stat.get("spend", 0.0)
            orders = stat.get("orders", 0)
            revenue = stat.get("revenue", 0.0)
            
            ctr = (clicks / impressions * 100) if impressions > 0 else 0.0
            cpc = (spend / clicks) if clicks > 0 else 0.0
            acos = (spend / revenue * 100) if revenue > 0 else 0.0
            roas = (revenue / spend) if spend > 0 else 0.0
            cpa = (spend / orders) if orders > 0 else 0.0
            
            ad_stat, created = await get_or_create(
                session,
                AdStats,
                defaults={
                    "impressions": impressions,
                    "clicks": clicks,
                    "spend": spend,
                    "orders": orders,
                    "revenue": revenue,
                    "ctr": ctr,
                    "cpc": cpc,
                    "acos": acos,
                    "roas": roas,
                    "cpa": cpa,
                    "synced_at": datetime.now(),
                },
                campaign_id=stat.get("campaign_id"),
                ads_id=stat.get("ads_id"),
                stat_date=datetime.strptime(stat.get("date", ""), "%Y-%m-%d").date() if stat.get("date") else date.today(),
            )
            
            if not created:
                # Обновляем статистику
                ad_stat.impressions = impressions
                ad_stat.clicks = clicks
                ad_stat.spend = spend
                ad_stat.orders = orders
                ad_stat.revenue = revenue
                ad_stat.ctr = ctr
                ad_stat.cpc = cpc
                ad_stat.acos = acos
                ad_stat.roas = roas
                ad_stat.cpa = cpa
                ad_stat.synced_at = datetime.now()
            
            saved_count += 1
            
        except Exception as e:
            logger.error(f"Ошибка сохранения статистики рекламы: {e}")
    
    return saved_count


# === Analytics Queries ===

async def get_unit_economics_summary(
    session: AsyncSession,
    period_start: date,
    period_end: date,
) -> dict:
    """
    Получить сводку unit-экономики за период.
    
    Args:
        session: Сессия БД
        period_start: Начало периода
        period_end: Конец периода
        
    Returns:
        Словарь с агрегированными метриками
    """
    query = select(
        func.sum(UnitEconomics.revenue).label("total_revenue"),
        func.sum(UnitEconomics.cogs).label("total_cogs"),
        func.sum(UnitEconomics.gross_profit).label("total_gross_profit"),
        func.sum(UnitEconomics.net_profit).label("total_net_profit"),
        func.sum(UnitEconomics.ozon_commission).label("total_commission"),
        func.sum(UnitEconomics.logistics_cost).label("total_logistics"),
        func.sum(UnitEconomics.ad_spend).label("total_ad_spend"),
        func.sum(UnitEconomics.tax_amount).label("total_tax"),
        func.avg(UnitEconomics.margin_percent).label("avg_margin"),
        func.count(UnitEconomics.id).label("sku_count"),
    ).where(
        UnitEconomics.period_start >= period_start,
        UnitEconomics.period_end <= period_end,
    )
    
    result = await session.execute(query)
    row = result.first()
    
    if not row:
        return {}
    
    return {
        "total_revenue": row.total_revenue or 0.0,
        "total_cogs": row.total_cogs or 0.0,
        "total_gross_profit": row.total_gross_profit or 0.0,
        "total_net_profit": row.total_net_profit or 0.0,
        "total_commission": row.total_commission or 0.0,
        "total_logistics": row.total_logistics or 0.0,
        "total_ad_spend": row.total_ad_spend or 0.0,
        "total_tax": row.total_tax or 0.0,
        "avg_margin": row.avg_margin or 0.0,
        "sku_count": row.sku_count or 0,
    }


async def get_ad_campaigns_performance(
    session: AsyncSession,
    period_start: date,
    period_end: date,
) -> List[dict]:
    """
    Получить эффективность рекламных кампаний за период.
    
    Args:
        session: Сессия БД
        period_start: Начало периода
        period_end: Конец периода
        
    Returns:
        Список словарей с метриками по кампаниям
    """
    query = select(
        AdCampaign.campaign_id,
        AdCampaign.name,
        func.sum(AdStats.impressions).label("total_impressions"),
        func.sum(AdStats.clicks).label("total_clicks"),
        func.sum(AdStats.spend).label("total_spend"),
        func.sum(AdStats.orders).label("total_orders"),
        func.sum(AdStats.revenue).label("total_revenue"),
    ).join(
        AdStats, AdCampaign.campaign_id == AdStats.campaign_id
    ).where(
        AdStats.stat_date >= period_start,
        AdStats.stat_date <= period_end,
    ).group_by(
        AdCampaign.campaign_id, AdCampaign.name
    )
    
    result = await session.execute(query)
    
    campaigns = []
    for row in result.all():
        spend = row.total_spend or 0.0
        revenue = row.total_revenue or 0.0
        clicks = row.total_clicks or 0
        impressions = row.total_impressions or 0
        orders = row.total_orders or 0
        
        campaigns.append({
            "campaign_id": row.campaign_id,
            "name": row.name,
            "impressions": impressions,
            "clicks": clicks,
            "spend": spend,
            "orders": orders,
            "revenue": revenue,
            "ctr": (clicks / impressions * 100) if impressions > 0 else 0.0,
            "acos": (spend / revenue * 100) if revenue > 0 else 0.0,
            "roas": (revenue / spend) if spend > 0 else 0.0,
            "cpa": (spend / orders) if orders > 0 else 0.0,
        })
    
    return campaigns
