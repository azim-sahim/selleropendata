# Ozon Analytics - SellerData аналог для анализа unit-экономики и рекламы

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Полноценное модульное приложение для продавцов Ozon, которое автоматически собирает данные через официальный Ozon API, рассчитывает unit-экономику по каждому SKU, формирует общую экономику бизнеса и анализирует эффективность рекламных кампаний.

## 📋 Возможности

### Unit-экономика по SKU
- Выручка, себестоимость, валовая прибыль
- Комиссия Ozon (настраиваемый % по категориям)
- Логистика Ozon (базовая ставка + вес × тариф)
- Эквайринг (2% от выручки)
- Реклама на SKU (расходы кампании / продажи)
- Налоги (УСН 6%, настраиваемо)
- Чистая прибыль и маржинальность %

### Общая экономика бизнеса
- Выручка, COGS, Gross Profit, Net Profit
- Cash Flow по периодам
- ROI рекламы, Break-even ACOS
- Динамика и тренды (WoW/MoM)

### Анализ рекламных кампаний
- Метрики: Impressions, Clicks, CTR, Spend, Orders, Revenue
- ACOS, ROAS, CPA, конверсии
- Рекомендации по оптимизации:
  - Отключение при ACOS > Break-even
  - Увеличение ставок при ROAS > 3.0
  - Пауза при CTR < 0.5%

## 🏗️ Архитектура проекта

```
ozon_analytics/
├── .env                          # Конфигурация (Client-ID, API-Key, настройки)
├── .env.example                  # Пример конфигурации
├── requirements.txt              # Зависимости Python
├── config.py                     # Настройки через Pydantic
├── main.py                       # Точка входа, запуск sync + web
├── api/
│   ├── __init__.py
│   └── ozon_client.py            # Асинхронный клиент Ozon API
├── sync/
│   ├── __init__.py
│   └── scheduler.py              # APScheduler, инкрементальная загрузка
├── analytics/
│   ├── __init__.py
│   └── engine.py                 # Расчеты unit-экономики, метрик
├── storage/
│   ├── __init__.py
│   ├── db.py                     # SQLAlchemy сессии, CRUD
│   └── models.py                 # SQLAlchemy модели данных
├── web/
│   ├── __init__.py
│   └── dashboard.py              # Streamlit интерфейс
├── data/
│   └── costs.csv                 # Себестоимость товаров (SKU, цена)
└── logs/
    └── ozon_analytics.log        # Логи приложения
```

## 📊 Endpoints Ozon API

| Модуль | Endpoint | Метод | Описание |
|--------|----------|-------|----------|
| **Товары** | `/v2/product/list` | POST | Список товаров |
| **Товары** | `/v2/product/info` | POST | Детали товара |
| **Заказы** | `/v2/posting/fbs/delivered` | POST | Доставленные заказы FBS |
| **Заказы** | `/v1/posting/fbs/get` | POST | Детали заказа |
| **Финансы** | `/v1/finance/cash-flow-statement` | POST | Отчет о cash flow |
| **Финансы** | `/v2/analytics/data` | POST | Аналитика продаж |
| **Реклама** | `/v1/campaign/list` | POST | Список кампаний |
| **Реклама** | `/v1/campaign/{id}/stats` | GET | Статистика кампании |
| **Реклама** | `/v1/ads/{id}/stats` | GET | Статистика объявлений |

## 🔧 Формулы расчета

### Unit-экономика
```python
# Выручка = кол-во проданных × цена продажи
revenue = quantity_sold * price_per_unit

# Себестоимость = кол-во × закупочная цена
cogs = quantity_sold * cost_per_unit

# Комиссия Ozon = Выручка × комиссия_%
ozon_commission = revenue * (commission_percent / 100)

# Логистика = базовая_ставка + (вес_кг × тариф_за_кг) × кол-во
logistics_cost = (logistics_base_rate + weight_kg * logistics_per_kg_rate) * quantity_sold

# Эквайринг = Выручка × 2.0%
ecwiring_cost = revenue * 0.02

# Налоги = Выручка × 6% (УСН)
tax_amount = revenue * 0.06

# Чистая прибыль = Выручка - Все расходы
net_profit = revenue - cogs - ozon_commission - logistics_cost - ecwiring_cost - ad_spend - tax_amount

# Маржа % = (Чистая прибыль / Выручка) × 100
margin_percent = (net_profit / revenue) * 100

# Break-even ACOS = Маржа до рекламы / Выручка × 100
break_even_acos = ((gross_profit - ozon_commission - logistics_cost - ecwiring_cost - tax_amount) / revenue) * 100
```

### Рекламные метрики
```python
CTR = clicks / impressions * 100
ACOS = spend / revenue * 100
ROAS = revenue / spend
CPA = spend / orders
ROI = (revenue - spend) / spend * 100
```

## 🚀 Быстрый старт

### 1. Клонирование и установка зависимостей

```bash
cd ozon_analytics
pip install -r requirements.txt
```

### 2. Настройка конфигурации

```bash
# Скопируйте пример конфига
cp .env.example .env

# Отредактируйте .env и укажите ваши credentials
nano .env  # или любой другой редактор
```

**Обязательные параметры:**
```env
OZON_CLIENT_ID=ваш_client_id
OZON_API_KEY=ваш_api_key
```

**Опциональные параметры (можно оставить по умолчанию):**
```env
DATABASE_URL=sqlite+aiosqlite:///./ozon_analytics.db
DEFAULT_COMMISSION_PERCENT=10.0
ECOMMERCE_FEE_PERCENT=2.0
TAX_PERCENT=6.0
LOGISTICS_BASE_RATE=50.0
LOGISTICS_PER_KG_RATE=30.0
OZON_RATE_LIMIT=15
SYNC_INTERVAL_MINUTES=60
```

### 3. Загрузка себестоимости товаров

Создайте файл `data/costs.csv` в формате:
```csv
sku,cost_price
123456,100.50
789012,250.00
```

### 4. Инициализация базы данных

```bash
python main.py --init-db
```

### 5. Первичная синхронизация данных

```bash
python main.py --sync
```

> ⚠️ **Важно:** Для реальной работы укажите valid Client-ID и API-Key в `.env`. 
> С тестовыми значениями синхронизация вернет ошибку авторизации.

### 6. Запуск веб-интерфейса

```bash
python main.py --web
```

Или напрямую:
```bash
streamlit run web/dashboard.py
```

Откройте браузер: **http://localhost:8501**

## 📖 Использование

### Команды main.py

```bash
# Инициализация БД
python main.py --init-db

# Однократная синхронизация
python main.py --sync

# Запуск планировщика (фоновая синхронизация)
python main.py --run-scheduler

# Запуск веб-интерфейса
python main.py --web

# Полный цикл (init + sync + web)
python main.py --all
```

### Планировщик задач

По умолчанию планировщик выполняет:
- **Товары**: каждые 6 часов
- **Заказы**: каждый час
- **Финансы**: раз в день в 02:00
- **Реклама**: каждые 4 часа
- **Unit-экономика**: раз в день в 03:00

Настройте интервалы в `.env`:
```env
SYNC_INTERVAL_MINUTES=60
```

## 🛡️ Обработка ошибок

Приложение реализует надежную обработку ошибок Ozon API:

- **429 Too Many Requests**: exponential backoff с ожиданием
- **401/403 Unauthorized**: логирование и остановка
- **5xx Server Errors**: автоматические retry (до 5 попыток)
- **Timeout**: повтор запроса с увеличенным таймаутом

Rate limiting настроен через asyncio.Semaphore (по умолчанию 15 req/sec).

## 📈 Веб-интерфейс

Dashboard предоставляет:

1. **KPI карточки**: Выручка, Прибыль, Маржа, ROAS
2. **Unit-экономика**: таблица с фильтрами по прибыльности
3. **Графики**: топ товаров по прибыли и маржинальности
4. **Реклама**: эффективность кампаний с рекомендациями

## 🔐 Безопасность

- API ключи хранятся только в `.env` (не коммитить в git!)
- Логи не содержат чувствительных данных
- SQLite база локальная (для production используйте PostgreSQL)

## 🚀 Масштабирование (Production)

Для production окружения рекомендуется:

1. **PostgreSQL вместо SQLite**:
   ```env
   DATABASE_URL=postgresql+asyncpg://user:pass@localhost/ozon_analytics
   ```

2. **Redis + Celery** для распределенных задач:
   ```bash
   pip install celery redis
   ```

3. **Docker контейнеризация**:
   ```dockerfile
   FROM python:3.12-slim
   WORKDIR /app
   COPY requirements.txt .
   RUN pip install -r requirements.txt
   COPY . .
   CMD ["python", "main.py", "--run-scheduler"]
   ```

4. **Мониторинг**:
   - Prometheus + Grafana для метрик
   - Sentry для отслеживания ошибок

5. **Nginx reverse proxy** для Streamlit

## 📝 Лицензия

MIT License - см. файл LICENSE для деталей.

## 🤝 Поддержка

Вопросы и предложения welcome в Issues/GitHub.
