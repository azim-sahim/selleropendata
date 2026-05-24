"""
Аналитический движок для расчета unit-экономики и бизнес-метрик.

Модуль реализует:
- Расчет unit-экономики по каждому SKU на основе РЕАЛЬНЫХ данных из Ozon API
- Автоматическое получение комиссий, логистики, эквайринга из финансовых операций
- Общую экономику бизнеса (выручка, прибыль, Cash Flow, ROI)
- Анализ рекламных кампаний (ROAS, ACOS, CTR, конверсии)
- Оптимизационные рекомендации

ВАЖНО: Все комиссии и сборы берутся из API Ozon (/v2/finance/operation-details),
а не рассчитываются по фиксированным процентам!
"""

import logging
from typing import List, Dict, Optional, Any, Tuple
from datetime import datetime, date, timedelta
from dataclasses import dataclass, field
from collections import defaultdict
import pandas as pd
import numpy as np

from config import config
from storage.models import UnitEconomics, Product, Order, AdStats, AdCampaign, FinanceOperation
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

logger = logging.getLogger(__name__)


@dataclass
class OrderFinancials:
    """
    Финансовые данные по одному заказу.
    
    Содержит РЕАЛЬНЫЕ значения из Ozon API:
    - commission_amount: фактическая комиссия Ozon
    - logistics_amount: фактическая стоимость логистики
    - ecwiring_amount: фактический эквайринг
    - other_deductions: прочие удержания (штрафы, возвраты)
    """
    posting_number: str
    sku: int
    product_name: str
    
    # Выручка
    revenue: float = 0.0
    price_per_unit: float = 0.0
    quantity: int = 1
    
    # Реальные расходы из API Ozon
    commission_amount: float = 0.0  # Комиссия Ozon (фактическая)
    logistics_amount: float = 0.0   # Логистика (фактическая)
    ecwiring_amount: float = 0.0    # Эквайринг (фактический)
    other_deductions: float = 0.0   # Прочие удержания
    
    # Себестоимость (загружается из CSV/справочника)
    cogs_amount: float = 0.0
    cost_per_unit: float = 0.0
    
    # Реклама (будет добавлена позже при агрегации)
    ad_spend: float = 0.0
    
    # Налоги (УСН 6% от выручки - это единственное расчетное значение)
    tax_amount: float = 0.0
    
    # Итоговые метрики
    gross_profit: float = 0.0  # Выручка - Себестоимость
    net_profit: float = 0.0    # Чистая прибыль
    
    def calculate(self):
        """
        Рассчитать метрики на основе реальных данных из API.
        
        Формулы:
        - Gross Profit = Выручка - Себестоимость
        - Tax = Выручка × 6% (УСН)
        - Net Profit = Выручка - Себестоимость - Комиссия - Логистика - Эквайринг - Прочее - Реклама - Налог
        """
        # Валовая прибыль
        self.gross_profit = self.revenue - self.cogs_amount
        
        # Налоги (УСН 6% от выручки - единственное расчетное значение)
        self.tax_amount = self.revenue * (config.tax_percent / 100.0)
        
        # Чистая прибыль
        self.net_profit = (
            self.revenue
            - self.cogs_amount
            - self.commission_amount
            - self.logistics_amount
            - self.ecwiring_amount
            - self.other_deductions
            - self.ad_spend
            - self.tax_amount
        )


@dataclass
class UnitEconomicsResult:
    """
    Результат расчета unit-экономики для одного SKU за период.
    
    Все финансовые метрики основаны на РЕАЛЬНЫХ данных из Ozon API.
    """
    
    sku: int
    product_name: str
    period_start: date
    period_end: date
    
    # Объемы
    quantity_sold: int = 0
    orders_count: int = 0  # Количество заказов
    
    # Выручка
    revenue: float = 0.0
    avg_price_per_unit: float = 0.0
    
    # Прямые затраты (себестоимость)
    cogs: float = 0.0
    avg_cost_per_unit: float = 0.0
    
    # Реальные комиссии и сборы Ozon (из API)
    ozon_commission: float = 0.0
    logistics_cost: float = 0.0
    ecwiring_cost: float = 0.0
    other_deductions: float = 0.0
    
    # Реклама
    ad_spend: float = 0.0
    ad_spend_per_unit: float = 0.0
    
    # Налоги
    tax_amount: float = 0.0
    
    # Итоговые метрики
    gross_profit: float = 0.0  # Выручка - Себестоимость
    net_profit: float = 0.0    # Чистая прибыль
    
    # Маржинальность (%)
    margin_percent: float = 0.0
    markup_percent: float = 0.0
    
    # Юнит-метрики (на 1 товар)
    unit_revenue: float = 0.0
    unit_cost: float = 0.0
    unit_profit: float = 0.0
    
    # Точка безубыточности по рекламе
    break_even_acos: float = 0.0
    
    # Эффективность рекламы
    acos_percent: float = 0.0
    roas: float = 0.0
    
    # Статус и рекомендации
    is_profitable: bool = True
    recommendation: str = ""
    
    # Детализация по комиссиям (для аналитики)
    commission_rate_effective: float = 0.0  # Фактический % комиссии от выручки
    logistics_per_unit: float = 0.0         # Фактическая логистика на единицу
    
    def add_order(self, order_fin: OrderFinancials):
        """Добавить данные заказа к агрегированным данным SKU."""
        self.quantity_sold += order_fin.quantity
        self.orders_count += 1
        self.revenue += order_fin.revenue
        self.cogs += order_fin.cogs_amount
        self.ozon_commission += order_fin.commission_amount
        self.logistics_cost += order_fin.logistics_amount
        self.ecwiring_cost += order_fin.ecwiring_amount
        self.other_deductions += order_fin.other_deductions
        self.ad_spend += order_fin.ad_spend
        self.tax_amount += order_fin.tax_amount
        self.gross_profit += order_fin.gross_profit
        self.net_profit += order_fin.net_profit
    
    def finalize(self, cfg: 'UnitEconomicsConfig'):
        """
        Завершить расчет метрик после агрегации всех заказов.
        
        Рассчитывает средние значения, маржинальность и рекомендации.
        """
        if self.quantity_sold == 0:
            self.recommendation = "Нет продаж за период"
            return
        
        # Средние значения
        self.avg_price_per_unit = self.revenue / self.quantity_sold
        self.avg_cost_per_unit = self.cogs / self.quantity_sold if self.cogs > 0 else 0.0
        self.ad_spend_per_unit = self.ad_spend / self.quantity_sold
        
        # Маржинальность
        if self.revenue > 0:
            self.margin_percent = (self.net_profit / self.revenue) * 100.0
            self.markup_percent = (self.net_profit / self.cogs) * 100.0 if self.cogs > 0 else 0.0
            
            # Фактический процент комиссии
            self.commission_rate_effective = (self.ozon_commission / self.revenue) * 100.0
            
            # Логистика на единицу
            self.logistics_per_unit = self.logistics_cost / self.quantity_sold
        
        # Юнит-метрики
        self.unit_revenue = self.avg_price_per_unit
        self.unit_cost = (
            self.avg_cost_per_unit
            + (self.ozon_commission / self.quantity_sold)
            + (self.logistics_cost / self.quantity_sold)
            + (self.ecwiring_cost / self.quantity_sold)
            + (self.other_deductions / self.quantity_sold)
            + (self.ad_spend / self.quantity_sold)
            + (self.tax_amount / self.quantity_sold)
        )
        self.unit_profit = self.unit_revenue - self.unit_cost
        
        # Break-even ACOS: максимальный ACOS при котором еще нет убытка
        # = (Валовая прибыль - все расходы кроме рекламы) / Выручка × 100
        pre_ad_profit = (
            self.gross_profit 
            - self.ozon_commission 
            - self.logistics_cost 
            - self.ecwiring_cost 
            - self.other_deductions 
            - self.tax_amount
        )
        self.break_even_acos = (pre_ad_profit / self.revenue) * 100.0 if self.revenue > 0 else 0.0
        
        # Текущий ACOS и ROAS
        if self.revenue > 0 and self.ad_spend > 0:
            self.acos_percent = (self.ad_spend / self.revenue) * 100.0
            self.roas = self.revenue / self.ad_spend
        
        # Прибыльность
        self.is_profitable = self.net_profit >= 0
        
        # Генерация рекомендаций
        self._generate_recommendations(cfg)
    
    def _generate_recommendations(self, cfg: 'UnitEconomicsConfig'):
        """Сгенерировать рекомендации на основе метрик."""
        recommendations = []
        
        # Проверка маржи
        if self.margin_percent < 0:
            recommendations.append(
                f"❌ Убыточный товар (маржа {self.margin_percent:.1f}%, убыток {self.net_profit:.0f}₽)"
            )
        elif self.margin_percent < cfg.min_margin_percent:
            recommendations.append(
                f"⚠️ Низкая маржа ({self.margin_percent:.1f}%). Рассмотрите повышение цены или снижение закупок"
            )
        
        # Проверка ACOS (только если были расходы на рекламу)
        if self.revenue > 0 and self.ad_spend > 0:
            if self.acos_percent > self.break_even_acos:
                recommendations.append(
                    f"❌ ACOS ({self.acos_percent:.1f}%) выше точки безубыточности ({self.break_even_acos:.1f}%). "
                    f"Реклама убыточная!"
                )
            elif self.acos_percent > cfg.high_acos_threshold:
                recommendations.append(
                    f"⚠️ Высокий ACOS ({self.acos_percent:.1f}%). Оптимизируйте рекламную кампанию"
                )
            elif self.roas > cfg.good_roas_threshold:
                recommendations.append(
                    f"✅ Отличный ROAS ({self.roas:.2f}). Можно масштабировать рекламу"
                )
        
        # Проверка структуры затрат
        if self.revenue > 0:
            # Если комиссия Ozon слишком высокая
            if self.commission_rate_effective > 20:
                recommendations.append(
                    f"⚠️ Высокая комиссия Ozon ({self.commission_rate_effective:.1f}%). "
                    f"Проверьте категорию товара"
                )
            
            # Если логистика съедает много прибыли
            logistics_share = (self.logistics_cost / self.revenue) * 100
            if logistics_share > 15:
                recommendations.append(
                    f"⚠️ Логистика составляет {logistics_share:.1f}% от выручки. "
                    f"Рассмотрите упаковку легче или фулфилмент ближе к клиентам"
                )
        
        # Если все хорошо
        if not recommendations:
            if self.is_profitable and self.margin_percent >= cfg.min_margin_percent:
                recommendations.append(
                    f"✅ Товар прибыльный (маржа {self.margin_percent:.1f}%, прибыль {self.net_profit:.0f}₽)"
                )
        
        self.recommendation = "; ".join(recommendations) if recommendations else "Нет рекомендаций"


@dataclass
class UnitEconomicsConfig:
    """Конфигурация порогов для рекомендаций."""
    
    # Пороги для рекомендаций
    min_margin_percent: float = 10.0  # Минимальная маржа для рекомендации "OK"
    high_acos_threshold: float = 30.0  # ACOS выше этого - проблема
    good_roas_threshold: float = 3.0  # ROAS выше этого - хорошо
    low_ctr_threshold: float = 0.5  # CTR ниже этого - плохо


class AnalyticsEngine:
    """
    Основной аналитический движок приложения.
    
    КЛЮЧЕВОЕ ОТЛИЧИЕ: Все комиссии и сборы берутся из реальных данных Ozon API,
    а не рассчитываются по фиксированным процентам.
    
    Предоставляет методы для:
    - Парсинга финансовых операций из Ozon API (комиссии, логистика, эквайринг)
    - Расчета unit-экономики по SKU на основе реальных данных
    - Анализа общей экономики бизнеса
    - Оценки эффективности рекламы
    """
    
    def __init__(self, session: AsyncSession):
        self.session = session
        self.cfg = UnitEconomicsConfig()
    
    async def parse_financial_operations(
        self,
        operations: List[Dict[str, Any]],
        products_map: Dict[int, Dict],
        costs_map: Optional[Dict[int, float]] = None,
    ) -> List[OrderFinancials]:
        """
        Распарсить финансовые операции из Ozon API в структурированные данные.
        
        Ozon API возвращает детализацию по каждому заказу через endpoint:
        POST /v2/finance/operation-details
        
        Типы операций в ответе:
        - "sale": Продажа товара (выручка)
        - "commission": Комиссия Ozon (удержание)
        - "logistics": Логистика (удержание)
        - "ecwiring": Эквайринг (удержание)
        - "penalty": Штрафы
        - "return": Возвраты
        
        Args:
            operations: Список операций из API Ozon
            products_map: Словарь {sku: product_info} для цен и названий
            costs_map: Словарь {sku: cost_price} для себестоимости
            
        Returns:
            Список OrderFinancials с реальными финансовыми данными
        """
        logger.info(f"Парсинг {len(operations)} финансовых операций")
        
        # Группируем операции по posting_number + sku
        order_data = defaultdict(lambda: {
            "revenue": 0.0,
            "commission": 0.0,
            "logistics": 0.0,
            "ecwiring": 0.0,
            "other": 0.0,
            "quantity": 0,
            "price": 0.0,
        })
        
        for op in operations:
            op_type = op.get("type", "")
            posting_number = op.get("posting_number", "")
            sku = op.get("product_sku") or op.get("sku")
            
            if not posting_number or not sku:
                continue
            
            key = (posting_number, int(sku))
            amount = abs(op.get("amount", 0.0))  # Берем абсолютное значение
            
            # Продукт информация
            product_info = products_map.get(int(sku), {})
            product_name = product_info.get("name", f"SKU {sku}")
            
            # Обновляем quantity и price из продукта
            if key not in order_data or order_data[key]["quantity"] == 0:
                order_data[key]["quantity"] = op.get("quantity", 1)
                order_data[key]["price"] = op.get("price", product_info.get("price", 0.0))
            
            # Классификация по типу операции
            # Положительные суммы - это выручка, отрицательные - удержания
            raw_amount = op.get("amount", 0.0)
            
            if op_type == "sale" or raw_amount > 0:
                # Выручка от продажи
                order_data[key]["revenue"] += raw_amount
            elif op_type == "commission":
                # Комиссия Ozon (всегда отрицательная в API)
                order_data[key]["commission"] += abs(raw_amount)
            elif op_type == "logistics":
                # Логистика
                order_data[key]["logistics"] += abs(raw_amount)
            elif op_type == "ecwiring" or "эквайринг" in op.get("description", "").lower():
                # Эквайринг
                order_data[key]["ecwiring"] += abs(raw_amount)
            else:
                # Прочие удержания (штрафы, возвраты, прочее)
                order_data[key]["other"] += abs(raw_amount)
        
        # Создаем OrderFinancials объекты
        results = []
        for (posting_number, sku), data in order_data.items():
            if data["revenue"] == 0 and data["quantity"] == 0:
                continue
            
            product_info = products_map.get(int(sku), {})
            product_name = product_info.get("name", f"SKU {sku}")
            
            # Себестоимость
            cost_per_unit = costs_map.get(int(sku)) if costs_map else product_info.get("cost_price", 0.0)
            cogs_amount = cost_per_unit * data["quantity"] if cost_per_unit else 0.0
            
            # Создаем объект
            order_fin = OrderFinancials(
                posting_number=posting_number,
                sku=int(sku),
                product_name=product_name,
                revenue=data["revenue"],
                price_per_unit=data["price"],
                quantity=data["quantity"],
                commission_amount=data["commission"],
                logistics_amount=data["logistics"],
                ecwiring_amount=data["ecwiring"],
                other_deductions=data["other"],
                cogs_amount=cogs_amount,
                cost_per_unit=cost_per_unit or 0.0,
            )
            
            # Рассчитываем метрики
            order_fin.calculate()
            results.append(order_fin)
        
        logger.info(f"Создано {len(results)} записей OrderFinancials")
        return results
    
    async def calculate_unit_economics_from_orders(
        self,
        order_financials: List[OrderFinancials],
        ad_spend_by_sku: Dict[int, float],
        period_start: date,
        period_end: date,
    ) -> List[UnitEconomicsResult]:
        """
        Рассчитать unit-экономику по SKU на основе реальных финансовых данных.
        
        Args:
            order_financials: Список распарсенных финансовых операций
            ad_spend_by_sku: Расходы на рекламу по SKU {sku: spend}
            period_start: Начало периода
            period_end: Конец периода
            
        Returns:
            Список результатов unit-экономики по каждому SKU
        """
        logger.info(f"Расчет unit-экономики за период {period_start} - {period_end}")
        
        # Агрегируем по SKU
        sku_results: Dict[int, UnitEconomicsResult] = {}
        
        for order_fin in order_financials:
            sku = order_fin.sku
            
            if sku not in sku_results:
                sku_results[sku] = UnitEconomicsResult(
                    sku=sku,
                    product_name=order_fin.product_name,
                    period_start=period_start,
                    period_end=period_end,
                )
            
            # Добавляем данные заказа к агрегированным
            sku_results[sku].add_order(order_fin)
        
        # Добавляем расходы на рекламу
        for sku, result in sku_results.items():
            ad_spend = ad_spend_by_sku.get(sku, 0.0)
            result.ad_spend = ad_spend
        
        # Финализируем расчеты (средние значения, маржинальность, рекомендации)
        for result in sku_results.values():
            result.finalize(self.cfg)
        
        results = list(sku_results.values())
        logger.info(f"Рассчитана unit-экономика для {len(results)} SKU")
        
        return results
    
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
        Устаревший метод для обратной совместимости.
        Используйте calculate_unit_economics_from_orders с реальными данными из API.
        """
        logger.warning(
            "Вызван устаревший метод calculate_unit_economics. "
            "Рекомендуется использовать calculate_unit_economics_from_orders с данными из /v2/finance/operation-details"
        )
        
        # Создаем products map
        products_map = {}
        for p in products:
            sku = p.get("sku") or p.get("id")
            if sku:
                products_map[int(sku)] = p
        
        # Имитируем финансовые операции из заказов (для обратной совместимости)
        # В реальном использовании нужно вызывать parse_financial_operations
        order_financials = []
        for order in orders:
            items = order.get("products", [])
            posting_number = order.get("posting_number", "unknown")
            
            for item in items:
                sku = item.get("sku") or item.get("product_id")
                if not sku:
                    continue
                
                sku = int(sku)
                product_info = products_map.get(sku, {})
                
                quantity = item.get("quantity", 1)
                price = item.get("price", product_info.get("price", 0.0))
                revenue = price * quantity
                
                # Себестоимость
                cost_per_unit = costs_map.get(sku) if costs_map else product_info.get("cost_price", 0.0)
                cogs = cost_per_unit * quantity if cost_per_unit else 0.0
                
                # ПРИМЕЧАНИЕ: Здесь мы НЕ можем получить реальные комиссии без вызова API
                # Поэтому используем заглушки. В production используйте parse_financial_operations!
                commission = revenue * 0.10  # Заглушка 10%
                logistics = revenue * 0.05   # Заглушка 5%
                ecwiring = revenue * 0.02    # Заглушка 2%
                
                order_fin = OrderFinancials(
                    posting_number=posting_number,
                    sku=sku,
                    product_name=product_info.get("name", f"SKU {sku}"),
                    revenue=revenue,
                    price_per_unit=price,
                    quantity=quantity,
                    commission_amount=commission,
                    logistics_amount=logistics,
                    ecwiring_amount=ecwiring,
                    other_deductions=0.0,
                    cogs_amount=cogs,
                    cost_per_unit=cost_per_unit or 0.0,
                )
                order_fin.calculate()
                order_financials.append(order_fin)
        
        # Группируем рекламу по SKU
        ad_by_sku = self._group_ad_spend_by_sku(ad_stats)
        
        # Рассчитываем unit-экономику
        return await self.calculate_unit_economics_from_orders(
            order_financials=order_financials,
            ad_spend_by_sku=ad_by_sku,
            period_start=period_start,
            period_end=period_end,
        )
    
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
