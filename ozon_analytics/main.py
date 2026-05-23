"""
Ozon Analytics - точка входа приложения.

Запуск:
    python main.py --init-db         # Инициализация БД
    python main.py --sync            # Однократная синхронизация
    python main.py --run-scheduler   # Запуск планировщика
    python main.py --web             # Запуск веб-интерфейса
"""

import asyncio
import argparse
import logging
import sys
from pathlib import Path
from datetime import datetime

from config import config
from storage.db import db_manager
from sync.scheduler import scheduler, get_scheduler


def setup_logging():
    """Настроить логирование."""
    # Создаем директорию для логов
    log_dir = Path(config.log_file).parent
    log_dir.mkdir(parents=True, exist_ok=True)
    
    # Настройка logging
    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(config.log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    
    logger = logging.getLogger(__name__)
    logger.info(f"Логирование настроено. Уровень: {config.log_level}, файл: {config.log_file}")
    
    return logger


def init_database():
    """Инициализировать базу данных."""
    logger = logging.getLogger(__name__)
    logger.info("Инициализация базы данных...")
    
    async def _init():
        await db_manager.init_db()
        logger.info("База данных успешно инициализирована")
    
    asyncio.run(_init())


def run_sync():
    """Выполнить однократную синхронизацию всех данных."""
    logger = logging.getLogger(__name__)
    logger.info("Запуск синхронизации данных...")
    
    async def _sync():
        try:
            results = await scheduler.run_full_sync()
            
            logger.info("=" * 50)
            logger.info("Результаты синхронизации:")
            logger.info("=" * 50)
            
            for sync_type, stats in results.items():
                if isinstance(stats, dict):
                    loaded = stats.get("loaded", stats.get("campaigns", stats.get("calculated", 0)))
                    error = stats.get("error")
                    status = "❌ FAILED" if error else "✅ OK"
                    logger.info(f"{sync_type}: {status} (загружено: {loaded})")
                    if error:
                        logger.error(f"  Ошибка: {error}")
                else:
                    logger.info(f"{sync_type}: {stats}")
            
            logger.info("=" * 50)
            
        except Exception as e:
            logger.error(f"Ошибка синхронизации: {e}", exc_info=True)
            raise
    
    asyncio.run(_sync())


def run_scheduler():
    """Запустить планировщик задач."""
    logger = logging.getLogger(__name__)
    logger.info("Запуск планировщика задач...")
    
    sched = get_scheduler()
    sched.start()
    
    logger.info(f"Планировщик запущен. Интервал синхронизации: {config.sync_interval_minutes} мин.")
    logger.info("Нажмите Ctrl+C для остановки")
    
    try:
        # Держим скрипт запущенным
        while True:
            asyncio.run(asyncio.sleep(1))
    except KeyboardInterrupt:
        logger.info("Остановка планировщика...")
        sched.stop()
        logger.info("Планировщик остановлен")


def run_web():
    """Запустить веб-интерфейс Streamlit."""
    import subprocess
    
    logger = logging.getLogger(__name__)
    logger.info("Запуск веб-интерфейса Streamlit...")
    
    # Запускаем streamlit как подпроцесс
    dashboard_path = Path(__file__).parent / "web" / "dashboard.py"
    
    if not dashboard_path.exists():
        logger.error(f"Файл dashboard.py не найден: {dashboard_path}")
        sys.exit(1)
    
    cmd = [
        sys.executable, "-m", "streamlit", "run",
        str(dashboard_path),
        "--server.address", "localhost",
        "--server.port", "8501",
        "--browser.gatherUsageStats", "false",
    ]
    
    logger.info(f"Команда: {' '.join(cmd)}")
    logger.info("Откройте браузер: http://localhost:8501")
    
    subprocess.run(cmd)


def main():
    """Основная функция."""
    parser = argparse.ArgumentParser(
        description="Ozon Analytics - система аналитики для продавцов Ozon",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры использования:
  python main.py --init-db              # Инициализировать базу данных
  python main.py --sync                 # Выполнить синхронизацию
  python main.py --run-scheduler        # Запустить планировщик
  python main.py --web                  # Запустить веб-интерфейс
  
  # Полный цикл для первого запуска:
  python main.py --init-db && python main.py --sync && python main.py --web
        """,
    )
    
    parser.add_argument(
        "--init-db",
        action="store_true",
        help="Инициализировать базу данных (создать таблицы)",
    )
    
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Выполнить однократную синхронизацию данных из Ozon API",
    )
    
    parser.add_argument(
        "--run-scheduler",
        action="store_true",
        help="Запустить планировщик задач (фоновая синхронизация)",
    )
    
    parser.add_argument(
        "--web",
        action="store_true",
        help="Запустить веб-интерфейс (Streamlit)",
    )
    
    parser.add_argument(
        "--all",
        action="store_true",
        help="Выполнить всё: init-db + sync + web",
    )
    
    args = parser.parse_args()
    
    # Настройка логирования
    logger = setup_logging()
    logger.info("=" * 60)
    logger.info("Ozon Analytics - система аналитики для Ozon sellers")
    logger.info("=" * 60)
    
    # Если нет аргументов, показать справку
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)
    
    try:
        if args.init_db or args.all:
            init_database()
        
        if args.sync or args.all:
            run_sync()
        
        if args.run_scheduler:
            run_scheduler()
        
        if args.web or args.all:
            run_web()
    
    except KeyboardInterrupt:
        logger.info("Приложение остановлено пользователем")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}", exc_info=True)
        sys.exit(1)
    
    logger.info("Приложение завершило работу")


if __name__ == "__main__":
    main()
