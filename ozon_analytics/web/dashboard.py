"""
Streamlit веб-интерфейс для дашборда Ozon Analytics.

Предоставляет:
- Обзор общей экономики бизнеса
- Unit-экономику по SKU с фильтрами
- Анализ рекламных кампаний
- Графики и визуализации через Plotly
"""

import logging
import asyncio
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any

import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
import pandas as pd
from sqlalchemy import select

from config import config
from storage.db import db_manager, get_unit_economics_summary, get_ad_campaigns_performance
from storage.models import Product, Order, AdCampaign, AdStats, UnitEconomics, SyncLog
from analytics.engine import AnalyticsEngine, load_costs_from_csv

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация страницы Streamlit
st.set_page_config(
    page_title="Ozon Analytics - Дашборд",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)


def init_session_state():
    """Инициализировать session state."""
    if "db_initialized" not in st.session_state:
        st.session_state.db_initialized = False
    if "data_loaded" not in st.session_state:
        st.session_state.data_loaded = False
    if "unit_results" not in st.session_state:
        st.session_state.unit_results = []
    if "business_summary" not in st.session_state:
        st.session_state.business_summary = {}


@st.cache_resource
def get_db_manager():
    """Получить менеджер БД (кэшируется)."""
    return db_manager


async def load_data_for_dashboard(
    period_start: date,
    period_end: date,
) -> tuple[List[Dict], Dict[str, Any], List[Dict]]:
    """
    Загрузить данные для дашборда.
    
    Returns:
        Кортеж (unit_results, business_summary, ad_performance)
    """
    async with db_manager.get_session() as session:
        # Получаем товары
        products_query = await session.execute(select(Product))
        products = [p.__dict__ for p in products_query.scalars().all()]
        
        # Получаем заказы за период
        orders_query = await session.execute(
            select(Order).where(Order.order_date >= datetime.combine(period_start, datetime.min.time()))
        )
        orders = [o.__dict__ for o in orders_query.scalars().all()]
        
        # Получаем статистику рекламы
        ads_query = await session.execute(
            select(AdStats).where(AdStats.stat_date >= period_start)
        )
        ad_stats = [a.__dict__ for a in ads_query.scalars().all()]
        
        # Загружаем себестоимость
        costs_map = load_costs_from_csv(config.cost_file_path)
        
        # Рассчитываем unit-экономику
        engine = AnalyticsEngine(session)
        unit_results = await engine.calculate_unit_economics(
            products=products,
            orders=orders,
            ad_stats=ad_stats,
            period_start=period_start,
            period_end=period_end,
            costs_map=costs_map,
        )
        
        # Получаем сводку бизнеса
        business_summary = await engine.get_business_summary(unit_results)
        
        # Получаем эффективность кампаний
        campaigns_query = await session.execute(select(AdCampaign))
        campaigns = [c.__dict__ for c in campaigns_query.scalars().all()]
        
        ad_performance = await engine.analyze_ad_campaigns(
            campaigns=campaigns,
            stats=ad_stats,
            period_start=period_start,
            period_end=period_end,
        )
        
        return unit_results, business_summary, ad_performance


def render_kpi_cards(business_summary: Dict[str, Any]):
    """Отрисовать карточки KPI."""
    if "error" in business_summary:
        st.warning("Нет данных для отображения")
        return
    
    cols = st.columns(5)
    
    # Выручка
    with cols[0]:
        revenue = business_summary.get("sales", {}).get("total_revenue", 0)
        st.metric(
            label="💰 Выручка",
            value=f"{revenue:,.0f} ₽",
        )
    
    # Чистая прибыль
    with cols[1]:
        net_profit = business_summary.get("profit", {}).get("net_profit", 0)
        delta_color = "normal" if net_profit >= 0 else "inverse"
        st.metric(
            label="📈 Чистая прибыль",
            value=f"{net_profit:,.0f} ₽",
            delta=f"{net_profit:,.0f} ₽",
            delta_color=delta_color,
        )
    
    # Маржинальность
    with cols[2]:
        margin = business_summary.get("profit", {}).get("margin_percent", 0)
        st.metric(
            label="📊 Маржинальность",
            value=f"{margin:.1f}%",
        )
    
    # Расходы на рекламу
    with cols[3]:
        ad_spend = business_summary.get("advertising", {}).get("total_spend", 0)
        st.metric(
            label="📢 Реклама",
            value=f"{ad_spend:,.0f} ₽",
        )
    
    # ROAS
    with cols[4]:
        roas = business_summary.get("advertising", {}).get("roas", 0)
        st.metric(
            label="🎯 ROAS",
            value=f"{roas:.2f}",
        )


def render_unit_economics_table(unit_results: List[Any]):
    """Отрисовать таблицу unit-экономики."""
    if not unit_results:
        st.info("Нет данных unit-экономики")
        return
    
    # Создаем DataFrame
    data = []
    for r in unit_results:
        data.append({
            "SKU": r.sku,
            "Товар": r.product_name[:50] + "..." if len(r.product_name) > 50 else r.product_name,
            "Продажи": r.quantity_sold,
            "Выручка": f"{r.revenue:,.0f}",
            "Себестоимость": f"{r.cogs:,.0f}",
            "Комиссия": f"{r.ozon_commission:,.0f}",
            "Логистика": f"{r.logistics_cost:,.0f}",
            "Реклама": f"{r.ad_spend:,.0f}",
            "Прибыль": f"{r.net_profit:,.0f}",
            "Маржа %": f"{r.margin_percent:.1f}",
            "Статус": "✅" if r.is_profitable else "❌",
            "Рекомендация": r.recommendation[:80] + "..." if len(r.recommendation) > 80 else r.recommendation,
        })
    
    df = pd.DataFrame(data)
    
    # Фильтры
    col1, col2 = st.columns(2)
    with col1:
        filter_profitable = st.selectbox(
            "Прибыльность",
            ["Все", "Прибыльные", "Убыточные"],
            key="filter_profitable"
        )
    with col2:
        search_sku = st.text_input("Поиск по SKU/названию", key="search_sku")
    
    # Применение фильтров
    if filter_profitable == "Прибыльные":
        df = df[df["Статус"] == "✅"]
    elif filter_profitable == "Убыточные":
        df = df[df["Статус"] == "❌"]
    
    if search_sku:
        df = df[
            df["SKU"].astype(str).str.contains(search_sku) |
            df["Товар"].str.contains(search_sku, case=False)
        ]
    
    # Отображение таблицы
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        height=400,
    )


def render_charts(unit_results: List[Any], business_summary: Dict[str, Any]):
    """Отрисовать графики."""
    if not unit_results:
        return
    
    # График: Топ товаров по прибыли
    top_tab, margin_tab = st.tabs(["🏆 Топ товаров", "📊 Маржинальность"])
    
    with top_tab:
        df = pd.DataFrame([{
            "SKU": r.sku,
            "Товар": r.product_name[:30],
            "Прибыль": r.net_profit,
            "Выручка": r.revenue,
        } for r in unit_results])
        
        df_sorted = df.sort_values("Прибыль", ascending=False).head(15)
        
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=df_sorted["Прибыль"],
            y=df_sorted["Товар"],
            orientation="h",
            marker=dict(
                color=df_sorted["Прибыль"].apply(lambda x: "green" if x > 0 else "red"),
            ),
        ))
        fig.update_layout(
            title="Топ-15 товаров по чистой прибыли",
            xaxis_title="Прибыль (₽)",
            height=500,
        )
        st.plotly_chart(fig, use_container_width=True)
    
    with margin_tab:
        df_margin = pd.DataFrame([{
            "SKU": r.sku,
            "Товар": r.product_name[:30],
            "Маржа %": r.margin_percent,
        } for r in unit_results])
        
        df_margin_sorted = df_margin.sort_values("Маржа %", ascending=False).head(15)
        
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=df_margin_sorted["Товар"],
            y=df_margin_sorted["Маржа %"],
            marker=dict(
                color=df_margin_sorted["Маржа %"].apply(
                    lambda x: "green" if x > 15 else ("orange" if x > 5 else "red")
                ),
            ),
        ))
        fig.update_layout(
            title="Топ-15 товаров по маржинальности",
            yaxis_title="Маржа (%)",
            height=500,
        )
        st.plotly_chart(fig, use_container_width=True)


def render_ad_performance(ad_performance: List[Dict]):
    """Отрисовать анализ рекламных кампаний."""
    if not ad_performance:
        st.info("Нет данных по рекламным кампаниям")
        return
    
    st.subheader("📢 Эффективность рекламных кампаний")
    
    # Таблица кампаний
    data = []
    for camp in ad_performance:
        metrics = camp.get("metrics", {})
        data.append({
            "Кампания": camp.get("name", "")[:40],
            "Расход": f"{metrics.get('spend', 0):,.0f}",
            "Заказы": metrics.get("orders", 0),
            "Выручка": f"{metrics.get('revenue', 0):,.0f}",
            "ROAS": f"{metrics.get('roas', 0):.2f}",
            "ACOS %": f"{metrics.get('acos_percent', 0):.1f}",
            "CTR %": f"{metrics.get('ctr_percent', 0):.2f}",
            "CPA": f"{metrics.get('cpa', 0):.0f}",
            "Рекомендации": "; ".join(camp.get("recommendations", [])[:2]),
        })
    
    df = pd.DataFrame(data)
    st.dataframe(df, use_container_width=True, hide_index=True, height=300)
    
    # График: ROAS vs ACOS
    if len(ad_performance) > 1:
        fig = px.scatter(
            pd.DataFrame([{
                "campaign": c.get("name", ""),
                "roas": c["metrics"].get("roas", 0),
                "acos": c["metrics"].get("acos_percent", 0),
                "spend": c["metrics"].get("spend", 0),
            } for c in ad_performance]),
            x="roas",
            y="acos",
            size="spend",
            hover_name="campaign",
            title="ROAS vs ACOS (размер = расходы)",
            labels={"roas": "ROAS", "acos": "ACOS (%)"},
        )
        fig.add_hline(y=30, line_dash="dash", annotation_text="High ACOS threshold")
        st.plotly_chart(fig, use_container_width=True)


def render_sync_status():
    """Отрисовать статус синхронизации."""
    st.sidebar.subheader("🔄 Статус синхронизации")
    
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def fetch_logs():
            async with db_manager.get_session() as session:
                query = await session.execute(select(SyncLog))
                logs = query.scalars().all()
                return logs
        
        logs = loop.run_until_complete(fetch_logs())
        loop.close()
        
        for log in logs:
            status_icon = "✅" if log.status == "success" else "❌"
            st.sidebar.markdown(
                f"{status_icon} **{log.sync_type}**: {log.last_sync_at.strftime('%d.%m %H:%M')}"
            )
            if log.records_loaded > 0:
                st.sidebar.caption(f"Загружено: {log.records_loaded}")
    
    except Exception as e:
        st.sidebar.warning(f"Не удалось получить статус: {e}")


def main():
    """Основная функция дашборда."""
    # Инициализация
    init_session_state()
    db_mgr = get_db_manager()
    
    # Заголовок
    st.title("📊 Ozon Analytics Dashboard")
    st.markdown("---")
    
    # Сайдбар с фильтрами
    st.sidebar.header("🔍 Фильтры")
    
    # Выбор периода
    default_end = date.today()
    default_start = default_end - timedelta(days=30)
    
    period_start = st.sidebar.date_input(
        "Начало периода",
        value=default_start,
        key="period_start"
    )
    period_end = st.sidebar.date_input(
        "Окончание периода",
        value=default_end,
        key="period_end"
    )
    
    # Кнопка обновления данных
    if st.sidebar.button("🔄 Обновить данные", type="primary"):
        st.session_state.data_loaded = False
        st.rerun()
    
    # Статус синхронизации
    render_sync_status()
    
    # Разделители в сайдбаре
    st.sidebar.markdown("---")
    
    # Инфо о приложении
    st.sidebar.markdown("""
    ### ℹ️ О приложении
    **Ozon Analytics** - аналог SellerData для анализа:
    - Unit-экономики по SKU
    - Общей экономики бизнеса
    - Эффективности рекламы
    
    [📖 Документация](README.md)
    """)
    
    # Основная часть
    if period_start >= period_end:
        st.error("Дата начала должна быть раньше даты окончания")
        return
    
    # Загрузка данных
    with st.spinner("Загрузка и расчет данных..."):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            unit_results, business_summary, ad_performance = loop.run_until_complete(
                load_data_for_dashboard(period_start, period_end)
            )
            
            loop.close()
            
            st.session_state.unit_results = unit_results
            st.session_state.business_summary = business_summary
            st.session_state.ad_performance = ad_performance
            st.session_state.data_loaded = True
            
        except Exception as e:
            st.error(f"Ошибка загрузки данных: {e}")
            logger.exception("Ошибка в дашборде")
            return
    
    if not st.session_state.data_loaded:
        st.info("Нажмите 'Обновить данные' для загрузки")
        return
    
    # KPI карточки
    render_kpi_cards(business_summary)
    
    st.markdown("---")
    
    # Вкладки
    tab1, tab2, tab3 = st.tabs(["📦 Unit-экономика", "📈 Графики", "📢 Реклама"])
    
    with tab1:
        st.subheader("Unit-экономика по товарам")
        render_unit_economics_table(unit_results)
    
    with tab2:
        st.subheader("Визуализация данных")
        render_charts(unit_results, business_summary)
    
    with tab3:
        render_ad_performance(ad_performance)


if __name__ == "__main__":
    main()
