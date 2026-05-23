"""
Аналитический движок для расчета unit-экономики и бизнес-метрик.

Модуль реализует:
- Расчет unit-экономики по каждому SKU
- Общую экономику бизнеса (выручка, прибыль, Cash Flow, ROI)
- Анализ рекламных кампаний (ROAS, ACOS, CTR, конверсии)
- Оптимизационные рекомендации
"""

import logging
from typing import List, Dict, Optional, Any
from datetime import datetime, date, timedelta
from dataclasses import dataclass, field
import pandas as pd
import numpy as np

from config import config
from storage.models import UnitEconomics, Product, Order, AdStats, AdCampaign
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

logger = logging.getLogger(__name__)


@dataclass
class UnitEconomicsConfig:
    """Конфигурация для расчета unit-экономики."""
    
    # Комиссии (в процентах)
    commission_percent: float = field(default_factory=lambda: config.default_commission_percent)
    ecommerce_fee_percent: float = field(default_factory=lambda: config.ecommerce_fee_percent)
    tax_percent: float = field(default_factory=lambda: config.tax_percent)
    
    # Логистика (в рублях)
    logistics_base_rate: float = field(default_factory=lambda: config.logistics_base_rate)
    logistics_per_kg_rate: float = field(default_factory=lambda: config.logistics_per_kg_rate)
    
    # Пороги для рекомендаций
    min_margin_percent: float = 10.0  # Минимальная маржа для рекомендации "OK"
    high_acos_threshold: float = 30.0  # ACOS выше этого - проблема
    good_roas_threshold: float = 3.0  # ROAS выше этого - хорошо
    low_ctr_threshold: float = 0.5  # CTR ниже этого - плохо


@dataclass
class UnitEconomicsResult:
    """Результат расчета unit-экономики для одного SKU."""
    
    sku: int
    product_name: str
    period_start: date
    period_end: date
    
    # Объемы
    quantity_sold: int = 0
    
    # Выручка
    revenue: float = 0.0
    price_per_unit: float = 0.0
    
    # Прямые затраты
    cogs: float = 0.0  # Себестоимость
    cost_per_unit: float = 0.0
    
    # Комиссии и сборы
    ozon_commission: float = 0.0
    logistics_cost: float = 0.0
    ecwiring_cost: float = 0.0
    
    # Реклама
    ad_spend: float = 0.0
    ad_spend_per_unit: float = 0.0
    
    # Налоги
    tax_amount: float = 0.0
    
    # Итоговые метрики
    gross_profit: float = 0.0  # Выручка - Себестоимость
    net_profit: float = 0.0  # Чистая прибыль
    
    # Маржинальность (%)
    margin_percent: float = 0.0
    markup_percent: float = 0.0
    
    # Юнит-метрики
    unit_revenue: float = 0.0
    unit_cost: float = 0.0
    unit_profit: float = 0.0
    
    # Точка безубыточности
    break_even_acos: float = 0.0
    
    # Статус и рекомендации
    is_profitable: bool = True
    recommendation: str = ""
    
    def calculate(self, cfg: UnitEconomicsConfig, weight_kg: float = 0.5):
        """
        Рассчитать все метрики на основе входных данных.
        
        Формулы:
        - Выручка = кол-во × цена
        - Себестоимость = кол-во × закупочная цена
        - Комиссия Ozon = Выручка × комиссия_%
        - Логистика = базовая_ставка + (вес × тариф_за_кг) × кол-во
        - Эквайринг = Выручка × 2%
        - Налоги = Выручка × 6%
        - Чистая прибыль = Выручка - Все расходы
        - Маржа % = (Чистая прибыль / Выручка) × 100
        """
        if self.quantity_sold == 0:
            self.recommendation = "Нет продаж за период"
            return
        
        # Выручка
        self.revenue = self.quantity_sold * self.price_per_unit
        
        # Себестоимость (COGS - Cost of Goods Sold)
        self.cogs = self.quantity_sold * self.cost_per_unit
        
        # Валовая прибыль
        self.gross_profit = self.revenue - self.cogs
        
        # Комиссия Ozon
        self.ozon_commission = self.revenue * (cfg.commission_percent / 100.0)
        
        # Логистика: базовая ставка + (вес × тариф) на единицу × количество
        logistics_per_item = cfg.logistics_base_rate + (weight_kg * cfg.logistics_per_kg_rate)
        self.logistics_cost = logistics_per_item * self.quantity_sold
        
        # Эквайринг
        self.ecwiring_cost = self.revenue * (cfg.ecommerce_fee_percent / 100.0)
        
        # Налоги (УСН 6% от выручки)
        self.tax_amount = self.revenue * (cfg.tax_percent / 100.0)
        
        # Чистая прибыль
        self.net_profit = (
            self.revenue
            - self.cogs
            - self.ozon_commission
            - self.logistics_cost
            - self.ecwiring_cost
            - self.ad_spend
            - self.tax_amount
        )
        
        # Маржинальность
        if self.revenue > 0:
            self.margin_percent = (self.net_profit / self.revenue) * 100.0
            self.markup_percent = (self.net_profit / self.cogs) * 100.0 if self.cogs > 0 else 0.0
        
        # Юнит-метрики (на 1 товар)
        self.unit_revenue = self.price_per_unit
        self.unit_cost = (
            self.cost_per_unit
            + (self.ozon_commission / self.quantity_sold)
            + (self.logistics_cost / self.quantity_sold)
            + (self.ecwiring_cost / self.quantity_sold)
            + (self.ad_spend / self.quantity_sold)
            + (self.tax_amount / self.quantity_sold)
        )
        self.unit_profit = self.unit_revenue - self.unit_cost
        
        # Точка безубыточности по рекламе (Break-even ACOS)
        # Максимальный ACOS, при котором еще нет убытка
        # Break-even ACOS = Маржа до рекламы / Выручка × 100
        pre_ad_profit = self.gross_profit - self.ozon_commission - self.logistics_cost - self.ecwiring_cost - self.tax_amount
        self.break_even_acos = (pre_ad_profit / self.revenue) * 100.0 if self.revenue > 0 else 0.0
        
        # Определение прибыльности
        self.is_profitable = self.net_profit >= 0
        
        # Генерация рекомендаций
        self._generate_recommendations(cfg)
    
    def _generate_recommendations(self, cfg: UnitEconomicsConfig):
        """Сгенерировать рекомендации на основе метрик."""
        recommendations = []
        
        # Проверка маржи
        if self.margin_percent < 0:
            recommendations.append(f"❌ Убыточный товар (маржа {self.margin_percent:.1f}%)")
        elif self.margin_percent < cfg.min_margin_percent:
            recommendations.append(f"⚠️ Низкая маржа ({self.margin_percent:.1f}%), рассмотрите повышение цены")
        
        # Проверка ACOS
        if self.revenue > 0 and self.ad_spend > 0:
            acos = (self.ad_spend / self.revenue) * 100
            if acos > self.break_even_acos:
                recommendations.append(f"❌ ACOS ({acos:.1f}%) выше точки безубыточности ({self.break_even_acos:.1f}%)")
            elif acos > cfg.high_acos_threshold:
                recommendations.append(f"⚠️ Высокий ACOS ({acos:.1f}%), оптимизируйте рекламу")
        
        # Если все хорошо
        if not recommendations:
            if self.is_profitable and self.margin_percent >= cfg.min_margin_percent:
                recommendations.append(f"✅ Товар прибыльный (маржа {self.margin_percent:.1f}%)")
        
        self.recommendation = "; ".join(recommendations) if recommendations else "Нет рекомендаций"


class AnalyticsEngine:
    """
    Основной аналитический движок приложения.
    
    Предоставляет методы для:
    - Расчета unit-экономики по SKU
    - Анализа общей экономики бизнеса
    - Оценки эффективности рекламы
    """
    
    def __init__(self, session: AsyncSession):
        self.session = session
        self.cfg = UnitEconomicsConfig()
    
    async def calculate_unit_economics(
        self,
        products: List[Dict[str, Any]],
        orders: List[Dict[str, Any]],
        ad_stats: List[Dict[str, Any]],
        period_start: date,
        period_end: date,
        costs_map: Optional[Dict[int, float]] = None,
    ) -> List[UnitEconomicsResult]:
        """
        Рассчитать unit-экономику для всех товаров.
        
        Args:
            products: Список товаров из БД/API
            orders: Список заказов за период
            ad_stats: Статистика рекламы за период
            period_start: Начало периода
            period_end: Конец периода
            costs_map: Словарь {sku: cost_price} для себестоимости
            
        Returns:
            Список результатов расчета по каждому SKU
        """
        logger.info(f"Расчет unit-экономики за период {period_start} - {period_end}")
        
        results = []
        
        # Группируем заказы по SKU
        orders_by_sku = self._group_orders_by_sku(orders)
        
        # Группируем рекламу по SKU
        ad_by_sku = self._group_ad_spend_by_sku(ad_stats)
        
        for product in products:
            sku = product.get("sku") or product.get("id")
            if not sku:
                continue
            
            # Данные о продажах
            sales_data = orders_by_sku.get(sku, {"quantity": 0, "revenue": 0.0})
            quantity_sold = sales_data["quantity"]
            
            if quantity_sold == 0:
                continue  # Пропускаем товары без продаж
            
            # Цены
            price_per_unit = product.get("price", 0.0)
            cost_per_unit = costs_map.get(sku, 0.0) if costs_map else product.get("cost_price", 0.0)
            
            # Вес товара (для логистики)
            weight_kg = product.get("weight", 0.5)
            
            # Реклама
            ad_spend = ad_by_sku.get(sku, 0.0)
            
            # Создаем результат
            result = UnitEconomicsResult(
                sku=sku,
                product_name=product.get("name", f"SKU {sku}"),
                period_start=period_start,
                period_end=period_end,
                quantity_sold=quantity_sold,
                price_per_unit=price_per_unit,
                cost_per_unit=cost_per_unit,
                ad_spend=ad_spend,
                ad_spend_per_unit=ad_spend / quantity_sold if quantity_sold > 0 else 0.0,
            )
            
            # Рассчитываем метрики
            result.calculate(self.cfg, weight_kg)
            results.append(result)
        
        logger.info(f"Рассчитана unit-экономика для {len(results)} SKU")
        return results
    
    def _group_orders_by_sku(self, orders: List[Dict]) -> Dict[int, Dict]:
        """Сгруппировать заказы по SKU."""
        grouped = {}
        
        for order in orders:
            items = order.get("products", [])
            for item in items:
                sku = item.get("sku") or item.get("product_id")
                if not sku:
                    continue
                
                if sku not in grouped:
                    grouped[sku] = {"quantity": 0, "revenue": 0.0}
                
                qty = item.get("quantity", 1)
                price = item.get("price", 0.0)
                
                grouped[sku]["quantity"] += qty
                grouped[sku]["revenue"] += qty * price
        
        return grouped
    
    def _group_ad_spend_by_sku(self, ad_stats: List[Dict]) -> Dict[int, float]:
        """Сгруппировать расходы на рекламу по SKU."""
        grouped = {}
        
        for stat in ad_stats:
            # SKU может быть в разных полях в зависимости от структуры ответа API
            sku = stat.get("sku") or stat.get("product_id")
            if not sku:
                continue
            
            spend = stat.get("spend", 0.0)
            grouped[sku] = grouped.get(sku, 0.0) + spend
        
        return grouped
    
    async def get_business_summary(
        self,
        unit_results: List[UnitEconomicsResult],
        finance_operations: Optional[List[Dict]] = None,
    ) -> Dict[str, Any]:
        """
        Получить сводку общей экономики бизнеса.
        
        Args:
            unit_results: Результаты unit-экономики
            finance_operations: Финансовые операции из API
            
        Returns:
            Словарь с агрегированными метриками бизнеса
        """
        if not unit_results:
            return {"error": "Нет данных для анализа"}
        
        # Агрегация по всем SKU
        total_revenue = sum(r.revenue for r in unit_results)
        total_cogs = sum(r.cogs for r in unit_results)
        total_gross_profit = sum(r.gross_profit for r in unit_results)
        total_net_profit = sum(r.net_profit for r in unit_results)
        total_commission = sum(r.ozon_commission for r in unit_results)
        total_logistics = sum(r.logistics_cost for r in unit_results)
        total_ecwiring = sum(r.ecwiring_cost for r in unit_results)
        total_ad_spend = sum(r.ad_spend for r in unit_results)
        total_tax = sum(r.tax_amount for r in unit_results)
        
        total_quantity = sum(r.quantity_sold for r in unit_results)
        profitable_skus = sum(1 for r in unit_results if r.is_profitable)
        unprofitable_skus = len(unit_results) - profitable_skus
        
        # Средняя маржа
        avg_margin = (total_net_profit / total_revenue * 100) if total_revenue > 0 else 0.0
        
        # ROI рекламы
        ad_roi = ((total_revenue - total_ad_spend) / total_ad_spend * 100) if total_ad_spend > 0 else 0.0
        
        # Cash Flow (если есть финансовые операции)
        cash_flow = 0.0
        if finance_operations:
            inflows = sum(op.get("amount", 0) for op in finance_operations if op.get("amount", 0) > 0)
            outflows = abs(sum(op.get("amount", 0) for op in finance_operations if op.get("amount", 0) < 0))
            cash_flow = inflows - outflows
        
        return {
            "period": {
                "start": unit_results[0].period_start.isoformat(),
                "end": unit_results[0].period_end.isoformat(),
            },
            "sales": {
                "total_revenue": round(total_revenue, 2),
                "total_quantity": total_quantity,
                "avg_order_value": round(total_revenue / total_quantity, 2) if total_quantity > 0 else 0.0,
            },
            "costs": {
                "cogs": round(total_cogs, 2),
                "commission": round(total_commission, 2),
                "logistics": round(total_logistics, 2),
                "ecwiring": round(total_ecwiring, 2),
                "ad_spend": round(total_ad_spend, 2),
                "tax": round(total_tax, 2),
                "total_costs": round(total_cogs + total_commission + total_logistics + total_ecwiring + total_ad_spend + total_tax, 2),
            },
            "profit": {
                "gross_profit": round(total_gross_profit, 2),
                "net_profit": round(total_net_profit, 2),
                "margin_percent": round(avg_margin, 2),
            },
            "cash_flow": round(cash_flow, 2),
            "advertising": {
                "total_spend": round(total_ad_spend, 2),
                "roi_percent": round(ad_roi, 2),
                "acos_percent": round((total_ad_spend / total_revenue * 100) if total_revenue > 0 else 0.0, 2),
                "roas": round((total_revenue / total_ad_spend) if total_ad_spend > 0 else 0.0, 2),
            },
            "products": {
                "total_skus": len(unit_results),
                "profitable_skus": profitable_skus,
                "unprofitable_skus": unprofitable_skus,
                "profitability_rate": round(profitable_skus / len(unit_results) * 100, 2) if unit_results else 0.0,
            },
        }
    
    async def analyze_ad_campaigns(
        self,
        campaigns: List[Dict],
        stats: List[Dict],
        period_start: date,
        period_end: date,
    ) -> List[Dict[str, Any]]:
        """
        Проанализировать эффективность рекламных кампаний.
        
        Args:
            campaigns: Список кампаний
            stats: Статистика по кампаниям
            period_start: Начало периода
            period_end: Конец периода
            
        Returns:
            Список анализов по каждой кампании с рекомендациями
        """
        logger.info(f"Анализ рекламных кампаний за период {period_start} - {period_end}")
        
        # Группируем статистику по campaign_id
        stats_by_campaign = {}
        for stat in stats:
            cid = stat.get("campaign_id")
            if not cid:
                continue
            
            if cid not in stats_by_campaign:
                stats_by_campaign[cid] = {
                    "impressions": 0,
                    "clicks": 0,
                    "spend": 0.0,
                    "orders": 0,
                    "revenue": 0.0,
                }
            
            stats_by_campaign[cid]["impressions"] += stat.get("impressions", 0)
            stats_by_campaign[cid]["clicks"] += stat.get("clicks", 0)
            stats_by_campaign[cid]["spend"] += stat.get("spend", 0.0)
            stats_by_campaign[cid]["orders"] += stat.get("orders", 0)
            stats_by_campaign[cid]["revenue"] += stat.get("revenue", 0.0)
        
        results = []
        
        for campaign in campaigns:
            cid = campaign.get("id")
            stats_data = stats_by_campaign.get(cid, {})
            
            impressions = stats_data.get("impressions", 0)
            clicks = stats_data.get("clicks", 0)
            spend = stats_data.get("spend", 0.0)
            orders = stats_data.get("orders", 0)
            revenue = stats_data.get("revenue", 0.0)
            
            # Расчет метрик
            ctr = (clicks / impressions * 100) if impressions > 0 else 0.0
            cpc = (spend / clicks) if clicks > 0 else 0.0
            acos = (spend / revenue * 100) if revenue > 0 else 0.0
            roas = (revenue / spend) if spend > 0 else 0.0
            cpa = (spend / orders) if orders > 0 else 0.0
            conversion_rate = (orders / clicks * 100) if clicks > 0 else 0.0
            
            # Расчет break-even ACOS на основе средней маржи
            break_even_acos = 30.0  # Можно рассчитать точнее на основе данных
            
            # Генерация рекомендаций
            recommendations = self._generate_ad_recommendations(
                ctr=ctr,
                acos=acos,
                roas=roas,
                break_even_acos=break_even_acos,
                spend=spend,
            )
            
            results.append({
                "campaign_id": cid,
                "name": campaign.get("name", f"Campaign {cid}"),
                "status": campaign.get("status", "unknown"),
                "metrics": {
                    "impressions": impressions,
                    "clicks": clicks,
                    "spend": round(spend, 2),
                    "orders": orders,
                    "revenue": round(revenue, 2),
                    "ctr_percent": round(ctr, 2),
                    "cpc": round(cpc, 2),
                    "acos_percent": round(acos, 2),
                    "roas": round(roas, 2),
                    "cpa": round(cpa, 2),
                    "conversion_rate_percent": round(conversion_rate, 2),
                },
                "break_even_acos": round(break_even_acos, 2),
                "recommendations": recommendations,
            })
        
        # Сортировка по расходу (сначала самые затратные)
        results.sort(key=lambda x: x["metrics"]["spend"], reverse=True)
        
        return results
    
    def _generate_ad_recommendations(
        self,
        ctr: float,
        acos: float,
        roas: float,
        break_even_acos: float,
        spend: float,
    ) -> List[str]:
        """Сгенерировать рекомендации по оптимизации рекламы."""
        recommendations = []
        
        # Проверка CTR
        if ctr < self.cfg.low_ctr_threshold:
            recommendations.append(
                f"⚠️ Низкий CTR ({ctr:.2f}%). Рекомендация: улучшите креативы, проверьте релевантность ключей"
            )
        
        # Проверка ACOS
        if acos > break_even_acos:
            recommendations.append(
                f"❌ ACOS ({acos:.1f}%) выше точки безубыточности ({break_even_acos:.1f}%). "
                f"Рекомендация: снизьте ставки или оптимизируйте объявления"
            )
        elif acos > self.cfg.high_acos_threshold:
            recommendations.append(
                f"⚠️ Высокий ACOS ({acos:.1f}%). Рассмотрите оптимизацию кампаний"
            )
        
        # Проверка ROAS
        if roas > self.cfg.good_roas_threshold:
            recommendations.append(
                f"✅ Отличный ROAS ({roas:.2f}). Рекомендация: увеличьте бюджет/ставки для масштабирования"
            )
        elif roas < 1.0 and spend > 0:
            recommendations.append(
                f"❌ ROAS ({roas:.2f}) меньше 1. Кампания убыточная. Рекомендация: пауза или полная переработка"
            )
        
        # Если расходов нет
        if spend == 0:
            recommendations.append("ℹ️ Нет расходов за период")
        
        if not recommendations:
            recommendations.append("✅ Кампания работает нормально")
        
        return recommendations
    
    def to_dataframe(self, results: List[UnitEconomicsResult]) -> pd.DataFrame:
        """
        Конвертировать результаты unit-экономики в DataFrame.
        
        Args:
            results: Список результатов
            
        Returns:
            pandas DataFrame
        """
        data = []
        for r in results:
            data.append({
                "sku": r.sku,
                "product_name": r.product_name,
                "quantity_sold": r.quantity_sold,
                "revenue": r.revenue,
                "cogs": r.cogs,
                "gross_profit": r.gross_profit,
                "ozon_commission": r.ozon_commission,
                "logistics_cost": r.logistics_cost,
                "ecwiring_cost": r.ecwiring_cost,
                "ad_spend": r.ad_spend,
                "tax_amount": r.tax_amount,
                "net_profit": r.net_profit,
                "margin_percent": r.margin_percent,
                "is_profitable": r.is_profitable,
                "recommendation": r.recommendation,
            })
        
        return pd.DataFrame(data)


def load_costs_from_csv(filepath: str) -> Dict[int, float]:
    """
    Загрузить себестоимость товаров из CSV файла.
    
    Ожидаемый формат CSV:
    sku,cost_price
    12345,100.50
    67890,250.00
    
    Args:
        filepath: Путь к CSV файлу
        
    Returns:
        Словарь {sku: cost_price}
    """
    try:
        df = pd.read_csv(filepath)
        costs = dict(zip(df["sku"], df["cost_price"]))
        logger.info(f"Загружена себестоимость для {len(costs)} товаров из {filepath}")
        return costs
    except FileNotFoundError:
        logger.warning(f"Файл себестоимости не найден: {filepath}")
        return {}
    except Exception as e:
        logger.error(f"Ошибка загрузки себестоимости: {e}")
        return {}
