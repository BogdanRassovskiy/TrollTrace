"""Дублирование уведомлений в Telegram-бот (README §4.2).

Нужны TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID (бот создаётся у @BotFather, chat_id — личный чат
исследователя или группа). Без них уведомления остаются только в панели.
"""

import logging

import httpx
from django.conf import settings
from django.utils import timezone

from .models import Notification

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"
ICONS = {
    Notification.Level.INFO: "✅",
    Notification.Level.WARNING: "⚠️",
    Notification.Level.CRITICAL: "🔴",
}
RETRY_WINDOW = timezone.timedelta(days=1)


def is_configured() -> bool:
    return bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID)


def format_message(notification: Notification) -> str:
    lines = [f"{ICONS.get(notification.level, '')} {notification.title}".strip()]
    if notification.text:
        lines.append(notification.text)
    if notification.url and settings.SITE_URL:
        lines.append(settings.SITE_URL.rstrip("/") + notification.url)
    return "\n".join(lines)[:4000]


def send(notification: Notification, transport: httpx.BaseTransport | None = None) -> bool:
    if not is_configured() or notification.sent_to_telegram:
        return False
    try:
        with httpx.Client(timeout=10, transport=transport) as client:
            response = client.post(
                API.format(token=settings.TELEGRAM_BOT_TOKEN),
                json={
                    "chat_id": settings.TELEGRAM_CHAT_ID,
                    "text": format_message(notification),
                    "disable_web_page_preview": True,
                },
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Telegram: upozornění %s nebylo odesláno: %s", notification.pk, exc)
        return False
    notification.sent_to_telegram = True
    notification.save(update_fields=["sent_to_telegram"])
    return True


def send_pending(transport: httpx.BaseTransport | None = None) -> int:
    """Повторная отправка того, что не ушло (сеть, Telegram недоступен), за последние сутки."""
    if not is_configured():
        return 0
    pending = Notification.objects.filter(
        sent_to_telegram=False,
        notification_type=Notification.Type.COLLECTION,
        created_at__gte=timezone.now() - RETRY_WINDOW,
    ).order_by("created_at")
    return sum(send(n, transport) for n in pending)
