"""
Конфигурация приложения через Pydantic Settings.
Загружает настройки из .env файла и переменных окружения.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator
from typing import Optional
import os


class OzonAnalyticsConfig(BaseSettings):
    """
    Основная конфигурация приложения Ozon Analytics.
    
    Все настройки загружаются из переменных окружения или .env файла.
    """
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )
    
    # === Ozon API Credentials ===
    ozon_client_id: str = Field(
        ...,
        description="Ozon Seller API Client ID",
        env="OZON_CLIENT_ID"
    )
    
    ozon_api_key: str = Field(
        ...,
        description="Ozon Seller API Key",
        env="OZON_API_KEY"
    )
    
    # === Database ===
    database_url: str = Field(
        default="sqlite+aiosqlite:///./ozon_analytics.db",
        description="Database connection URL",
        env="DATABASE_URL"
    )
    
    # === Commission and Fees (percent) ===
    default_commission_percent: float = Field(
        default=10.0,
        ge=0.0,
        le=100.0,
        description="Комиссия Ozon по умолчанию (%)",
        env="DEFAULT_COMMISSION_PERCENT"
    )
    
    ecommerce_fee_percent: float = Field(
        default=2.0,
        ge=0.0,
        le=100.0,
        description="Эквайринг (%)",
        env="ECOMMERCE_FEE_PERCENT"
    )
    
    tax_percent: float = Field(
        default=6.0,
        ge=0.0,
        le=100.0,
        description="Налог УСН (%)",
        env="TAX_PERCENT"
    )
    
    # === Logistics Rates (RUB) ===
    logistics_base_rate: float = Field(
        default=50.0,
        ge=0.0,
        description="Базовая ставка логистики (руб)",
        env="LOGISTICS_BASE_RATE"
    )
    
    logistics_per_kg_rate: float = Field(
        default=30.0,
        ge=0.0,
        description="Ставка логистики за кг (руб/кг)",
        env="LOGISTICS_PER_KG_RATE"
    )
    
    # === API Rate Limiting ===
    ozon_rate_limit: int = Field(
        default=15,
        ge=1,
        le=100,
        description="Лимит запросов к Ozon API в секунду",
        env="OZON_RATE_LIMIT"
    )
    
    ozon_timeout: int = Field(
        default=30,
        ge=5,
        le=300,
        description="Таймаут запроса к Ozon API (сек)",
        env="OZON_TIMEOUT"
    )
    
    ozon_max_retries: int = Field(
        default=5,
        ge=0,
        le=10,
        description="Максимальное количество повторных попыток",
        env="OZON_MAX_RETRIES"
    )
    
    ozon_backoff_factor: float = Field(
        default=2.0,
        ge=1.0,
        le=10.0,
        description="Множитель exponential backoff",
        env="OZON_BACKOFF_FACTOR"
    )
    
    # === Logging ===
    log_level: str = Field(
        default="INFO",
        description="Уровень логирования",
        env="LOG_LEVEL"
    )
    
    log_file: str = Field(
        default="logs/ozon_analytics.log",
        description="Путь к файлу логов",
        env="LOG_FILE"
    )
    
    # === Scheduler ===
    sync_interval_minutes: int = Field(
        default=60,
        ge=5,
        le=1440,
        description="Интервал синхронизации (минуты)",
        env="SYNC_INTERVAL_MINUTES"
    )
    
    # === Cost File ===
    cost_file_path: str = Field(
        default="data/costs.csv",
        description="Путь к CSV файлу с себестоимостью товаров",
        env="COST_FILE_PATH"
    )
    
    # === Application ===
    app_name: str = Field(
        default="Ozon Analytics",
        description="Название приложения",
        env="APP_NAME"
    )
    
    debug: bool = Field(
        default=False,
        description="Режим отладки",
        env="DEBUG"
    )
    
    @field_validator('log_level')
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Валидация уровня логирования."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v.upper() not in valid_levels:
            raise ValueError(f"Недопустимый уровень логирования: {v}. Допустимые: {valid_levels}")
        return v.upper()
    
    @property
    def ozon_base_url(self) -> str:
        """Базовый URL Ozon Seller API."""
        return "https://api-seller.ozon.ru"
    
    @property
    def ozon_headers(self) -> dict:
        """Заголовки для запросов к Ozon API."""
        return {
            "Client-Id": self.ozon_client_id,
            "Api-Key": self.ozon_api_key,
            "Content-Type": "application/json"
        }
    
    def get_commission_by_category(self, category: str) -> float:
        """
        Получить комиссию по категории товара.
        
        В production здесь должна быть логика загрузки комиссий из конфига
        или базы данных. Для демо возвращаем значение по умолчанию.
        
        Args:
            category: Категория товара
            
        Returns:
            Процент комиссии
        """
        # Пример маппинга категорий (можно расширить через конфиг)
        category_commissions = {
            "Электроника": 8.0,
            "Одежда": 15.0,
            "Дом и сад": 12.0,
            "Книги": 5.0,
        }
        return category_commissions.get(category, self.default_commission_percent)


# Глобальный экземпляр конфигурации
config = OzonAnalyticsConfig()
