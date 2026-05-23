"""
Асинхронный клиент Ozon Seller API.

Реализует:
- Авторизацию через Client-Id и Api-Key
- Rate limiting с использованием semaphore
- Exponential backoff при ошибках 429/5xx
- Pagination для больших ответов
- Логирование всех запросов
- Получение фактических комиссий, логистики и эквайринга из API
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional, TypeVar, Generic
from datetime import datetime, date, timedelta
import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    retry_if_result,
)
from pydantic import BaseModel

from config import config

logger = logging.getLogger(__name__)

T = TypeVar('T', bound=BaseModel)


class OzonAPIError(Exception):
    """Базовое исключение для ошибок Ozon API."""
    
    def __init__(self, message: str, status_code: Optional[int] = None, response: Optional[Dict] = None):
        self.message = message
        self.status_code = status_code
        self.response = response or {}
        super().__init__(self.message)


class OzonRateLimitError(OzonAPIError):
    """Превышен лимит запросов (429)."""
    pass


class OzonAuthError(OzonAPIError):
    """Ошибка авторизации (401/403)."""
    pass


class OzonClient:
    """
    Асинхронный клиент для работы с Ozon Seller API.
    
    Особенности:
    - Автоматический rate limiting через asyncio.Semaphore
    - Exponential backoff при временных ошибках
    - Поддержка pagination
    - Детальное логирование
    - Получение фактических комиссий и сборов из API
    """
    
    BASE_URL = "https://api-seller.ozon.ru"
    
    # === Products Endpoints ===
    ENDPOINT_PRODUCT_LIST = "/v2/product/list"
    ENDPOINT_PRODUCT_INFO = "/v2/product/info"
    ENDPOINT_PRODUCT_PRICES = "/v2/prices/info"
    
    # === Orders Endpoints (FBS/FBO) ===
    ENDPOINT_POSTING_UNFULFILLED = "/v2/posting/fbs/unfulfilled"
    ENDPOINT_POSTING_DELIVERED = "/v2/posting/fbs/delivered"
    ENDPOINT_POSTING_GET = "/v1/posting/fbs/get"
    ENDPOINT_POSTING_FBO_GET = "/v3/posting/fbo/get"
    
    # === Finance Endpoints ===
    ENDPOINT_CASH_FLOW = "/v1/finance/cash-flow-statement"
    ENDPOINT_ANALYTICS_DATA = "/v2/analytics/data"
    ENDPOINT_FINANCE_TRANSACTIONS = "/v1/finance/transaction-list"
    ENDPOINT_FINANCIAL_DETAILS = "/v2/finance/operation-details"
    
    # === Advertising Endpoints ===
    ENDPOINT_CAMPAIGN_LIST = "/v1/campaign/list"
    ENDPOINT_CAMPAIGN_STATS = "/v1/campaign/{campaign_id}/stats"
    ENDPOINT_ADS_STATS = "/v1/ads/{ads_id}/stats"
    
    # === Reports Endpoints (для детализации комиссий) ===
    ENDPOINT_REPORTS_LIST = "/v1/reports/list"
    ENDPOINT_REPORTS_DOWNLOAD = "/v1/reports/download/{report_id}"
    
    def __init__(self):
        self.client_id = config.ozon_client_id
        self.api_key = config.ozon_api_key
        self.timeout = config.ozon_timeout
        self.max_retries = config.ozon_max_retries
        self.backoff_factor = config.ozon_backoff_factor
        
        # Semaphore для rate limiting
        self._semaphore = asyncio.Semaphore(config.ozon_rate_limit)
        
        # HTTP клиент
        self._client: Optional[httpx.AsyncClient] = None
    
    @property
    def headers(self) -> Dict[str, str]:
        """Заголовки для всех запросов к Ozon API."""
        return {
            "Client-Id": self.client_id,
            "Api-Key": self.api_key,
            "Content-Type": "application/json"
        }
    
    async def _get_client(self) -> httpx.AsyncClient:
        """Получить или создать HTTP клиент."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout=self.timeout, connect=10.0),
                limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
            )
        return self._client
    
    async def close(self):
        """Закрыть HTTP клиент."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
    
    def _is_retryable_error(self, response: httpx.Response) -> bool:
        """Проверка, является ли ошибка временной и стоит ли повторять запрос."""
        # 429 Too Many Requests - обязательно повторяем
        if response.status_code == 429:
            return True
        # 5xx Server Errors - повторяем
        if 500 <= response.status_code < 600:
            return True
        return False
    
    def _should_retry_with_result(self, result: httpx.Response) -> bool:
        """Tenacity predicate для проверки результата."""
        return self._is_retryable_error(result)
    
    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2.0, min=1.0, max=60.0),
        retry=(
            retry_if_exception_type((httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError)) |
            retry_if_result(_should_retry_with_result)
        ),
        reraise=True
    )
    async def _request_with_retry(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
    ) -> httpx.Response:
        """
        Выполнить HTTP запрос с автоматическими повторами.
        
        Args:
            method: HTTP метод (GET, POST)
            endpoint: Путь endpoint относительно BASE_URL
            params: Query параметры
            json_data: JSON тело запроса
            
        Returns:
            httpx.Response объект
            
        Raises:
            OzonAPIError: При критических ошибках
        """
        url = f"{self.BASE_URL}{endpoint}"
        client = await self._get_client()
        
        logger.debug(f"Запрос к Ozon API: {method} {url}")
        
        try:
            async with self._semaphore:  # Rate limiting
                response = await client.request(
                    method=method,
                    url=url,
                    headers=self.headers,
                    params=params,
                    json=json_data,
                )
                
                # Логирование ответа
                logger.debug(f"Ответ Ozon API: {response.status_code} для {endpoint}")
                
                # Проверка на ошибки авторизации
                if response.status_code in (401, 403):
                    raise OzonAuthError(
                        f"Ошибка авторизации: {response.status_code}",
                        status_code=response.status_code,
                        response=response.json() if response.content else {}
                    )
                
                # Проверка на rate limit
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After", "5")
                    logger.warning(f"Rate limit превышен. Retry-After: {retry_after} сек")
                    raise OzonRateLimitError(
                        f"Превышен лимит запросов. Подождите {retry_after} сек",
                        status_code=429,
                        response={"retry_after": retry_after}
                    )
                
                # Проверка на другие ошибки
                if response.status_code >= 400:
                    error_detail = response.text[:500] if response.content else "No details"
                    logger.error(f"Ошибка Ozon API {response.status_code}: {error_detail}")
                    raise OzonAPIError(
                        f"Ozon API вернул ошибку {response.status_code}: {error_detail}",
                        status_code=response.status_code,
                        response={}
                    )
                
                return response
                
        except httpx.ConnectError as e:
            logger.error(f"Ошибка подключения к Ozon API: {e}")
            raise
        except httpx.ReadTimeout as e:
            logger.error(f"Таймаут чтения от Ozon API: {e}")
            raise
        except httpx.RemoteProtocolError as e:
            logger.error(f"Ошибка протокола Ozon API: {e}")
            raise
    
    async def request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Выполнить запрос к Ozon API и вернуть JSON ответ.
        
        Args:
            method: HTTP метод
            endpoint: Путь endpoint
            params: Query параметры
            json_data: JSON тело запроса
            
        Returns:
            Словарь с данными ответа
        """
        response = await self._request_with_retry(method, endpoint, params, json_data)
        return response.json() if response.content else {}
    
    async def request_paginated(
        self,
        endpoint: str,
        json_data: Dict[str, Any],
        limit_field: str = "limit",
        offset_field: str = "offset",
        result_field: str = "result",
        items_field: str = "items",
        max_pages: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Выполнить paginated запрос к Ozon API.
        
        Многие endpoints Ozon возвращают данные страницами. Этот метод
        автоматически загружает все страницы.
        
        Args:
            endpoint: Путь endpoint
            json_data: Базовые параметры запроса
            limit_field: Название поля для лимита страниц
            offset_field: Название поля для смещения
            result_field: Поле в ответе с результатами
            items_field: Поле внутри result со списком элементов
            max_pages: Максимальное количество страниц для загрузки
            
        Returns:
            Список всех элементов со всех страниц
        """
        all_items = []
        offset = 0
        limit = json_data.get(limit_field, 100)
        page = 0
        
        while page < max_pages:
            # Подготовка параметров для текущей страницы
            request_data = json_data.copy()
            request_data[limit_field] = limit
            request_data[offset_field] = offset
            
            logger.debug(f"Загрузка страницы {page + 1}, offset={offset}, limit={limit}")
            
            response = await self._request_with_retry("POST", endpoint, json_data=request_data)
            
            result = response.get(result_field, {})
            items = result.get(items_field, [])
            
            if not items:
                logger.info(f"Страница {page + 1} пуста, завершаем загрузку")
                break
            
            all_items.extend(items)
            logger.info(f"Загружено {len(items)} элементов на странице {page + 1}, всего: {len(all_items)}")
            
            # Проверка, есть ли еще данные
            total_count = result.get("total_count", len(items))
            if len(all_items) >= total_count:
                logger.info(f"Загружены все данные: {len(all_items)} из {total_count}")
                break
            
            offset += limit
            page += 1
            
            # Небольшая пауза между запросами страниц
            await asyncio.sleep(0.1)
        
        return all_items
    
    # === Products Methods ===
    
    async def get_product_list(
        self,
        last_id: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """
        Получить список товаров.
        
        Endpoint: /v2/product/list
        
        Args:
            last_id: ID последнего товара из предыдущего запроса (для pagination)
            limit: Количество товаров на странице (макс 1000)
            
        Returns:
            Ответ API со списком товаров
        """
        json_data = {
            "last_id": last_id or "",
            "limit": min(limit, 1000)
        }
        return await self.request("POST", self.ENDPOINT_PRODUCT_LIST, json_data=json_data)
    
    async def get_all_products(self) -> List[Dict[str, Any]]:
        """
        Получить все товары с pagination.
        
        Returns:
            Список всех товаров
        """
        all_products = []
        last_id = ""
        
        while True:
            response = await self.get_product_list(last_id=last_id, limit=1000)
            result = response.get("result", {})
            items = result.get("items", [])
            
            if not items:
                break
            
            all_products.extend(items)
            logger.info(f"Загружено товаров: {len(all_products)}")
            
            # Проверка на последнюю страницу
            if len(items) < 1000:
                break
            
            last_id = items[-1].get("id", "")
            await asyncio.sleep(0.1)
        
        return all_products
    
    async def get_product_info(self, product_id: Optional[int] = None, sku: Optional[int] = None) -> Dict[str, Any]:
        """
        Получить детальную информацию о товаре.
        
        Endpoint: /v2/product/info
        
        Args:
            product_id: ID товара на Ozon
            sku: Артикул товара
            
        Returns:
            Информация о товаре
        """
        json_data = {}
        if product_id:
            json_data["product_id"] = product_id
        elif sku:
            json_data["sku"] = sku
        else:
            raise ValueError("Необходимо указать product_id или sku")
        
        return await self.request("POST", self.ENDPOINT_PRODUCT_INFO, json_data=json_data)
    
    # === Orders Methods ===
    
    async def get_unfulfilled_orders(
        self,
        since: Optional[datetime] = None,
        to: Optional[datetime] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Получить необработанные заказы FBS.
        
        Endpoint: /v2/posting/fbs/unfulfilled
        
        Args:
            since: Начальная дата
            to: Конечная дата
            limit: Лимит заказов
            
        Returns:
            Список заказов
        """
        json_data = {
            "since": since.isoformat() if since else None,
            "to": to.isoformat() if to else None,
            "limit": limit,
        }
        # Убираем None значения
        json_data = {k: v for k, v in json_data.items() if v is not None}
        
        return await self.request("POST", self.ENDPOINT_POSTING_UNFULFILLED, json_data=json_data)
    
    async def get_delivered_orders(
        self,
        since: Optional[datetime] = None,
        to: Optional[datetime] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Получить доставленные заказы FBS.
        
        Endpoint: /v2/posting/fbs/delivered
        
        Args:
            since: Начальная дата
            to: Конечная дата
            limit: Лимит заказов
            
        Returns:
            Список заказов
        """
        json_data = {
            "since": since.isoformat() if since else None,
            "to": to.isoformat() if to else None,
            "limit": limit,
        }
        json_data = {k: v for k, v in json_data.items() if v is not None}
        
        return await self.request("POST", self.ENDPOINT_POSTING_DELIVERED, json_data=json_data)
    
    async def get_order_details(self, posting_number: str) -> Dict[str, Any]:
        """
        Получить детали заказа по номеру.
        
        Endpoint: /v1/posting/fbs/get
        
        Args:
            posting_number: Номер заказа (posting number)
            
        Returns:
            Детали заказа
        """
        json_data = {"posting_number": posting_number}
        return await self.request("POST", self.ENDPOINT_POSTING_GET, json_data=json_data)
    async def get_fbo_order_details(self, posting_number: str) -> Dict[str, Any]:
        """
        Получить детали заказа FBO по номеру.

        Endpoint: /v3/posting/fbo/get

        Args:
            posting_number: Номер заказа FBO

        Returns:
            Детали заказа FBO
        """
        json_data = {"posting_number": posting_number}
        return await self.request("POST", self.ENDPOINT_POSTING_FBO_GET, json_data=json_data)

    async def get_financial_operation_details(
        self,
        posting_number: str,
        operation_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Получить детализацию финансовых операций по заказу.

        Endpoint: /v2/finance/operation-details
        
        Это основной endpoint для получения реальных комиссий!
        
        Возвращает детальную информацию по всем начислениям и удержаниям:
        - Продажа товара (выручка)
        - Комиссия Ozon (по категории товара)
        - Логистика (базовая + за кг)
        - Эквайринг (2%)
        - Обработка возврата
        - Штрафы
        - Прочие удержания

        Args:
            posting_number: Номер заказа
            operation_type: Тип операции (опционально)

        Returns:
            Список финансовых операций с деталями
        """
        json_data = {
            "posting_number": posting_number,
        }
        if operation_type:
            json_data["operation_type"] = operation_type
        
        response = await self.request("POST", self.ENDPOINT_FINANCIAL_DETAILS, json_data=json_data)
        return response.get("result", {}).get("operations", [])

    async def get_finance_transactions(
        self,
        date_from: datetime,
        date_to: datetime,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """
        Получить список финансовых транзакций.

        Endpoint: /v1/finance/transaction-list

        Args:
            date_from: Начальная дата
            date_to: Конечная дата
            limit: Лимит записей
            offset: Смещение

        Returns:
            Список транзакций
        """
        json_data = {
            "date": {
                "from": date_from.strftime("%Y-%m-%d"),
                "to": date_to.strftime("%Y-%m-%d")
            },
            "limit": limit,
            "offset": offset,
        }
        return await self.request("POST", self.ENDPOINT_FINANCE_TRANSACTIONS, json_data=json_data)

    async def get_all_finance_transactions(
        self,
        date_from: datetime,
        date_to: datetime,
        max_pages: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Получить все финансовые транзакции за период с pagination.

        Args:
            date_from: Начальная дата
            date_to: Конечная дата
            max_pages: Максимум страниц

        Returns:
            Список всех транзакций
        """
        all_transactions = []
        offset = 0
        limit = 100
        page = 0
        
        while page < max_pages:
            response = await self.get_finance_transactions(
                date_from=date_from,
                date_to=date_to,
                limit=limit,
                offset=offset,
            )
            
            operations = response.get("result", {}).get("operations", [])
            if not operations:
                break
            
            all_transactions.extend(operations)
            logger.info(f"Загружено {len(operations)} транзакций на странице {page + 1}, всего: {len(all_transactions)}")
            
            # Проверка на последнюю страницу
            if len(operations) < limit:
                break
            
            offset += limit
            page += 1
            await asyncio.sleep(0.1)
        
        return all_transactions

    
    # === Finance Methods ===
    
    async def get_cash_flow_statement(
        self,
        date_from: datetime,
        date_to: datetime,
    ) -> Dict[str, Any]:
        """
        Получить отчет о движении денежных средств.
        
        Endpoint: /v1/finance/cash-flow-statement
        
        Args:
            date_from: Начальная дата периода
            date_to: Конечная дата периода
            
        Returns:
            Отчет о cash flow
        """
        json_data = {
            "date": {
                "from": date_from.strftime("%Y-%m-%d"),
                "to": date_to.strftime("%Y-%m-%d")
            }
        }
        return await self.request("POST", self.ENDPOINT_CASH_FLOW, json_data=json_data)
    
    async def get_analytics_data(
        self,
        date_from: datetime,
        date_to: datetime,
        metrics: Optional[List[str]] = None,
        dimensions: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Получить аналитику продаж.
        
        Endpoint: /v2/analytics/data
        
        Args:
            date_from: Начальная дата
            date_to: Конечная дата
            metrics: Список метрик
            dimensions: Список измерений
            
        Returns:
            Данные аналитики
        """
        default_metrics = [
            "revenue",
            "orders",
            "hits",
            "cart_hits",
            "position_category",
        ]
        
        json_data = {
            "date": {
                "from": date_from.strftime("%Y-%m-%d"),
                "to": date_to.strftime("%Y-%m-%d")
            },
            "metrics": metrics or default_metrics,
            "dimensions": dimensions or ["day"],
            "sort": [{"key": "day", "order": "ASC"}],
            "limit": 1000,
            "offset": 0,
        }
        
        return await self.request_paginated(
            self.ENDPOINT_ANALYTICS_DATA,
            json_data,
            result_field="data",
            items_field="data"
        )
    
    # === Advertising Methods ===
    
    async def get_campaign_list(self) -> List[Dict[str, Any]]:
        """
        Получить список рекламных кампаний.
        
        Endpoint: /v1/campaign/list
        
        Returns:
            Список кампаний
        """
        json_data = {}
        response = await self.request("POST", self.ENDPOINT_CAMPAIGN_LIST, json_data=json_data)
        return response.get("result", {}).get("campaigns", [])
    
    async def get_campaign_stats(self, campaign_id: int, date_from: datetime, date_to: datetime) -> Dict[str, Any]:
        """
        Получить статистику рекламной кампании.
        
        Endpoint: /v1/campaign/{campaign_id}/stats
        
        Args:
            campaign_id: ID кампании
            date_from: Начальная дата
            date_to: Конечная дата
            
        Returns:
            Статистика кампании
        """
        endpoint = self.ENDPOINT_CAMPAIGN_STATS.format(campaign_id=campaign_id)
        params = {
            "dateFrom": date_from.strftime("%Y-%m-%d"),
            "dateTo": date_to.strftime("%Y-%m-%d"),
        }
        return await self.request("GET", endpoint, params=params)
    
    async def get_ads_stats(self, ads_id: int, date_from: datetime, date_to: datetime) -> Dict[str, Any]:
        """
        Получить статистику объявлений.
        
        Endpoint: /v1/ads/{ads_id}/stats
        
        Args:
            ads_id: ID объявления
            date_from: Начальная дата
            date_to: Конечная дата
            
        Returns:
            Статистика объявлений
        """
        endpoint = self.ENDPOINT_ADS_STATS.format(ads_id=ads_id)
        params = {
            "dateFrom": date_from.strftime("%Y-%m-%d"),
            "dateTo": date_to.strftime("%Y-%m-%d"),
        }
        return await self.request("GET", endpoint, params=params)


# Глобальный экземпляр клиента (lazy initialization)
_ozon_client: Optional[OzonClient] = None


def get_ozon_client() -> OzonClient:
    """Получить экземпляр Ozon клиента."""
    global _ozon_client
    if _ozon_client is None:
        _ozon_client = OzonClient()
    return _ozon_client


async def close_ozon_client():
    """Закрыть соединение Ozon клиента."""
    global _ozon_client
    if _ozon_client:
        await _ozon_client.close()
        _ozon_client = None
