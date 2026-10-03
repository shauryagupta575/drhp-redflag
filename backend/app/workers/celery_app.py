from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery("drhp", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.broker_connection_retry_on_startup = True
