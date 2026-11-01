from celery import shared_task


@shared_task
def ping() -> str:
    """Проверка связки Django ↔ Celery ↔ брокер."""
    return "pong"
