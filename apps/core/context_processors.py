from django.conf import settings


def project(request):
    """Общие для всех шаблонов значения."""
    return {"PROJECT_VERSION": settings.TROLLTRACE["VERSION"]}
