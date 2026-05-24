"""
Планировщик задач синхронизации данных с Ozon API.

Использует APScheduler для периодического выполнения:
- Синхронизация товаров
- Синхронизация заказов
- Синхронизация финансов
- Синхронизация рекламы
- Расчет unit-экономики
"""

import logging
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any
import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import config
from api.ozon_client import get_ozon_client, close_ozon_client
from storage.db import db_manager, save_sync_log, get_last_sync_date
from storage.models import Product
from analytics.engine import AnalyticsEngine, load_costs_from_csv

logger = logging.getLogger(__name__)


class SyncScheduler:
    """
    Планировщик синхронизации данных.
    
    Управляет периодическими задачами по загрузке данных из Ozon API
    и расчету аналитики.
    """
    
    def __init__(self):
        self.scheduler = AsyncIOScheduler(
            timezone="Europe/Moscow",
            job_defaults={
                "coalesce": True,  # Объединять пропущенные запуски
                "max_instances": 1,  # Только один экземпляр задачи одновременно
                "misfire_grace_time": 60,  # Допустимое опоздание в секундах
            }
        )
        self._is_running = False
    
    def start(self):
        """Запустить планировщик."""
        if not self._is_running:
            self._register_jobs()
            self.scheduler.start()
            self._is_running = True
            logger.info("Планировщик синхронизации запущен")
    
    def stop(self):
        """Остановить планировщик."""
        if self._is_running:
            self.scheduler.shutdown(wait=True)
            self._is_running = False
            logger.info("Планировщик синхронизации остановлен")
    
    def _register_jobs(self):
        """Зарегистрировать все задачи."""
        # Синхронизация товаров - каждые 6 часов
        self.scheduler.add_job(
            self.sync_products,
            trigger=IntervalTrigger(hours=6),
            id="sync_products",
            name="Синхронизация товаров",
            replace_existing=True,
        )
        
        # Синхронизация заказов - каждый час
        self.scheduler.add_job(
            self.sync_orders,
            trigger=IntervalTrigger(hours=1),
            id="sync_orders",
            name="Синхронизация заказов",
            replace_existing=True,
        )
        
        # Синхронизация финансов - раз в день в 02:00
        self.scheduler.add_job(
            self.sync_finance,
            trigger=CronTrigger(hour=2, minute=0),
            id="sync_finance",
            name="Синхронизация финансов",
            replace_existing=True,
        )
        
        # Синхронизация рекламы - каждые 4 часа
        self.scheduler.add_job(
            self.sync_advertising,
            trigger=IntervalTrigger(hours=4),
            id="sync_advertising",
            name="Синхронизация рекламы",
            replace_existing=True,
        )
        
        # Расчет unit-экономики - раз в день в 03:00
        self.scheduler.add_job(
            self.calculate_unit_economics_daily,
            trigger=CronTrigger(hour=3, minute=0),
            id="calculate_unit_economics",
            name="Расчет unit-экономики",
            replace_existing=True,
        )
        
        logger.info(f"Зарегистрировано {len(self.scheduler.get_jobs())} задач синхронизации")
    
    async def sync_products(self) -> Dict[str, Any]:
        """
        Синхронизировать товары из Ozon API.
        
        Returns:
            Статистика синхронизации
        """
        logger.info("Начало синхронизации товаров")
        stats = {"loaded": 0, "updated": 0, "failed": 0, "error": None}
        
        try:
            ozon_client = get_ozon_client()
            
            async with db_manager.get_session() as session:
                # Получаем все товары из API
                products_data = await ozon_client.get_all_products()
                
                if not products_data:
                    logger.warning("Товары не найдены или ошибка API")
                    await save_sync_log(
                        session,
                        "products",
                        "partial",
                        error_message="Нет данных от API",
                    )
                    return stats
                
                # Сохраняем в БД
                from storage.db import save_products
                stats["loaded"] = await save_products(session, products_data)
                
                # Лог успешной синхронизации
                await save_sync_log(
                    session,
                    "products",
                    "success",
                    records_loaded=stats["loaded"],
                    last_successful_date=date.today(),
                )
                
                logger.info(f"Синхронизировано {stats['loaded']} товаров")
                
        except Exception as e:
            logger.error(f"Ошибка синхронизации товаров: {e}", exc_info=True)
            stats["error"] = str(e)
            
            async with db_manager.get_session() as session:
                await save_sync_log(
                    session,
                    "products",
                    "failed",
                    error_message=str(e),
                )
        
        finally:
            await close_ozon_client()
        
        return stats
    
    async def sync_orders(self, days_back: int = 7) -> Dict[str, Any]:
        """
        Синхронизировать заказы из Ozon API.
        
        Args:
            days_back: За сколько дней загружать заказы
            
        Returns:
            Статистика синхронизации
        """
        logger.info(f"Начало синхронизации заказов (за {days_back} дн.)")
        stats = {"loaded": 0, "updated": 0, "failed": 0, "error": None}
        
        try:
            ozon_client = get_ozon_client()
            
            since = datetime.now() - timedelta(days=days_back)
            
            async with db_manager.get_session() as session:
                # Загружаем доставленные заказы
                orders_data = await ozon_client.get_delivered_orders(
                    since=since,
                    limit=100,
                )
                
                if orders_data:
                    from storage.db import save_orders
                    stats["loaded"] = await save_orders(session, orders_data)
                
                # Лог синхронизации
                await save_sync_log(
                    session,
                    "orders",
                    "success" if stats["loaded"] > 0 else "partial",
                    records_loaded=stats["loaded"],
                    last_successful_date=date.today(),
                )
                
                logger.info(f"Синхронизировано {stats['loaded']} заказов")
                
        except Exception as e:
            logger.error(f"Ошибка синхронизации заказов: {e}", exc_info=True)
            stats["error"] = str(e)
            
            async with db_manager.get_session() as session:
                await save_sync_log(
                    session,
                    "orders",
                    "failed",
                    error_message=str(e),
                )
        
        finally:
            await close_ozon_client()
        
        return stats
    
    async def sync_finance(self, days_back: int = 30) -> Dict[str, Any]:
        """
        Синхронизировать финансовые операции из Ozon API.
        
        ВАЖНО: Здесь загружаются детальные финансовые операции по каждому заказу
        через endpoint /v2/finance/operation-details для точного расчета комиссий.
        
        Args:
            days_back: За сколько дней загружать данные
            
        Returns:
            Статистика синхронизации
        """
        logger.info(f"Начало синхронизации финансов (за {days_back} дн.)")
        stats = {"loaded": 0, "updated": 0, "failed": 0, "error": None}
        
        try:
            ozon_client = get_ozon_client()
            
            date_to = datetime.now()
            date_from = date_to - timedelta(days=days_back)
            
            async with db_manager.get_session() as session:
                # 1. Сначала получаем список заказов за период
                orders_data = await ozon_client.get_delivered_orders(
                    since=date_from,
                    limit=500,  # Берем больше заказов
                )
                
                all_operations = []
                posting_numbers = set()
                
                # 2. Для каждого заказа получаем детализацию финансовых операций
                # Это ключевой момент для получения реальных комиссий!
                for order in orders_data:
                    posting_number = order.get("posting_number")
                    if not posting_number or posting_number in posting_numbers:
                        continue
                    
                    posting_numbers.add(posting_number)
                    
                    try:
                        # Получаем детальную финансовую информацию по заказу
                        # Endpoint: /v2/finance/operation-details
                        operations = await ozon_client.get_financial_operation_details(
                            posting_number=posting_number,
                        )
                        
                        if operations:
                            all_operations.extend(operations)
                            logger.debug(f"Загружено {len(operations)} операций для заказа {posting_number}")
                        
                        # Небольшая пауза для соблюдения rate limits
                        await asyncio.sleep(0.05)
                        
                    except Exception as e:
                        logger.warning(f"Не удалось получить детали для заказа {posting_number}: {e}")
                        continue
                
                logger.info(f"Всего загружено {len(all_operations)} финансовых операций")
                
                # 3. Сохраняем в БД
                if all_operations:
                    from storage.db import save_finance_operations
                    stats["loaded"] = await save_finance_operations(session, all_operations)
                
                # Также сохраняем общий cash flow statement для сверки
                cash_flow_data = await ozon_client.get_cash_flow_statement(
                    date_from=date_from,
                    date_to=date_to,
                )
                
                # Лог синхронизации
                await save_sync_log(
                    session,
                    "finance",
                    "success" if stats["loaded"] > 0 else "partial",
                    records_loaded=stats["loaded"],
                    last_successful_date=date.today(),
                )
                
                logger.info(f"Синхронизировано {stats['loaded']} финансовых операций")
                
        except Exception as e:
            logger.error(f"Ошибка синхронизации финансов: {e}", exc_info=True)
            stats["error"] = str(e)
            
            async with db_manager.get_session() as session:
                await save_sync_log(
                    session,
                    "finance",
                    "failed",
                    error_message=str(e),
                )
        
        finally:
            await close_ozon_client()
        
        return stats
    
    async def sync_advertising(self, days_back: int = 14) -> Dict[str, Any]:
        """
        Синхронизировать рекламные кампании и статистику.
        
        Args:
            days_back: За сколько дней загружать статистику
            
        Returns:
            Статистика синхронизации
        """
        logger.info(f"Начало синхронизации рекламы (за {days_back} дн.)")
        stats = {"campaigns": 0, "stats_records": 0, "error": None}
        
        try:
            ozon_client = get_ozon_client()
            
            date_to = datetime.now()
            date_from = date_to - timedelta(days=days_back)
            
            async with db_manager.get_session() as session:
                # Получаем список кампаний
                campaigns = await ozon_client.get_campaign_list()
                
                if campaigns:
                    from storage.db import save_ad_campaigns, save_ad_stats
                    stats["campaigns"] = await save_ad_campaigns(session, campaigns)
                    
                    # Получаем статистику по каждой кампании
                    all_stats = []
                    for campaign in campaigns:
                        cid = campaign.get("id")
                        if not cid:
                            continue
                        
                        try:
                            campaign_stats = await ozon_client.get_campaign_stats(
                                campaign_id=cid,
                                date_from=date_from,
                                date_to=date_to,
                            )
                            
                            # Преобразуем в формат для сохранения
                            if campaign_stats:
                                for day_stat in campaign_stats.get("days", []):
                                    all_stats.append({
                                        "campaign_id": cid,
                                        "date": day_stat.get("date"),
                                        "impressions": day_stat.get("impressions", 0),
                                        "clicks": day_stat.get("clicks", 0),
                                        "spend": day_stat.get("spend", 0.0),
                                        "orders": day_stat.get("orders", 0),
                                        "revenue": day_stat.get("revenue", 0.0),
                                    })
                        except Exception as e:
                            logger.warning(f"Ошибка загрузки статистики кампании {cid}: {e}")
                    
                    if all_stats:
                        stats["stats_records"] = await save_ad_stats(session, all_stats)
                
                # Лог синхронизации
                await save_sync_log(
                    session,
                    "ads",
                    "success" if stats["campaigns"] > 0 else "partial",
                    records_loaded=stats["campaigns"] + stats["stats_records"],
                    last_successful_date=date_to.date(),
                )
                
                logger.info(f"Синхронизировано {stats['campaigns']} кампаний, {stats['stats_records']} записей статистики")
                
        except Exception as e:
            logger.error(f"Ошибка синхронизации рекламы: {e}", exc_info=True)
            stats["error"] = str(e)
            
            async with db_manager.get_session() as session:
                await save_sync_log(
                    session,
                    "ads",
                    "failed",
                    error_message=str(e),
                )
        
        finally:
            await close_ozon_client()
        
        return stats
    
    async def calculate_unit_economics_daily(self, days_back: int = 1) -> Dict[str, Any]:
        """
        Рассчитать unit-экономику за прошедший день.
        
        Args:
            days_back: За сколько дней рассчитывать
            
        Returns:
            Результаты расчета
        """
        logger.info(f"Расчет unit-экономики за {days_back} дн.")
        results = {"calculated": 0, "error": None}
        
        try:
            async with db_manager.get_session() as session:
                # Получаем данные из БД
                from sqlalchemy import select
                from storage.models import Product, Order, AdStats
                
                # Товары
                products_query = await session.execute(select(Product))
                products = [p.__dict__ for p in products_query.scalars().all()]
                
                # Заказы за период
                period_end = datetime.now()
                period_start = period_end - timedelta(days=days_back)
                
                orders_query = await session.execute(
                    select(Order).where(Order.order_date >= period_start)
                )
                orders = [o.__dict__ for o in orders_query.scalars().all()]
                
                # Статистика рекламы
                ads_query = await session.execute(
                    select(AdStats).where(AdStats.stat_date >= period_start.date())
                )
                ad_stats = [a.__dict__ for a in ads_query.scalars().all()]
                
                # Загружаем себестоимость из CSV
                costs_map = load_costs_from_csv(config.cost_file_path)
                
                # Рассчитываем unit-экономику
                engine = AnalyticsEngine(session)
                unit_results = await engine.calculate_unit_economics(
                    products=products,
                    orders=orders,
                    ad_stats=ad_stats,
                    period_start=period_start.date(),
                    period_end=period_end.date(),
                    costs_map=costs_map,
                )
                
                results["calculated"] = len(unit_results)
                logger.info(f"Рассчитана unit-экономика для {results['calculated']} SKU")
                
        except Exception as e:
            logger.error(f"Ошибка расчета unit-экономики: {e}", exc_info=True)
            results["error"] = str(e)
        
        return results
    
    async def run_full_sync(self) -> Dict[str, Any]:
        """
        Выполнить полную синхронизацию всех данных.
        
        Используется для первоначальной загрузки или ручного запуска.
        
        Returns:
            Общая статистика
        """
        logger.info("Запуск полной синхронизации")
        
        results = {
            "products": await self.sync_products(),
            "orders": await self.sync_orders(days_back=30),
            "finance": await self.sync_finance(days_back=30),
            "ads": await self.sync_advertising(days_back=30),
            "unit_economics": await self.calculate_unit_economics_daily(days_back=30),
        }
        
        logger.info("Полная синхронизация завершена")
        return results


# Глобальный экземпляр планировщика
scheduler = SyncScheduler()


def get_scheduler() -> SyncScheduler:
    """Получить экземпляр планировщика."""
    return scheduler
