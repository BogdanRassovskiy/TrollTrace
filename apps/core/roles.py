"""Роли доступа (README §7.2 «Доступ»): исследователь и наблюдатель.

Роль — группа Django. Исследователь видит рабочую часть и панель и может менять данные;
наблюдатель (например, руководитель) видит только рабочую часть. Суперпользователь
считается исследователем.
"""

from django.contrib.auth.mixins import AccessMixin
from django.contrib.auth.models import Group, Permission

RESEARCHER = "Výzkumník"
OBSERVER = "Pozorovatel"
# Разметчик валидационной выборки (этап 10): видит только интерфейс разметки, вслепую.
ANNOTATOR = "Anotátor"
ROLES = {"researcher": RESEARCHER, "observer": OBSERVER, "annotator": ANNOTATOR}
# Прежние (русские) названия групп: при migrate переименовываются, участники сохраняются.
LEGACY_NAMES = {"Исследователь": RESEARCHER, "Наблюдатель": OBSERVER}

# Приложения с исследовательскими данными: исследователю на них даются все права.
RESEARCH_APPS = [
    "actors",
    "documents",
    "ingestion",
    "narratives",
    "analytics",
    "panel",
    "validation",
]
# Журнал аудита не правится никем, даже исследователем.
READ_ONLY_MODELS = {("panel", "auditlog")}


def _in_group(user, name: str) -> bool:
    if not user.is_authenticated:
        return False
    cache = getattr(user, "_role_names", None)
    if cache is None:
        cache = user._role_names = set(user.groups.values_list("name", flat=True))
    return name in cache


def is_researcher(user) -> bool:
    return user.is_authenticated and (user.is_superuser or _in_group(user, RESEARCHER))


def can_view_dashboard(user) -> bool:
    return is_researcher(user) or _in_group(user, OBSERVER)


def can_annotate(user) -> bool:
    return is_researcher(user) or _in_group(user, ANNOTATOR)


def role_label(user) -> str:
    if is_researcher(user):
        return RESEARCHER
    if _in_group(user, OBSERVER):
        return OBSERVER
    if _in_group(user, ANNOTATOR):
        return ANNOTATOR
    return "Bez role"


def ensure_roles(**kwargs) -> None:
    """Создаёт группы и выставляет их права. Идемпотентно, вызывается после migrate."""
    for old, new in LEGACY_NAMES.items():
        if not Group.objects.filter(name=new).exists():
            Group.objects.filter(name=old).update(name=new)
    researcher, _ = Group.objects.get_or_create(name=RESEARCHER)
    observer, _ = Group.objects.get_or_create(name=OBSERVER)
    Group.objects.get_or_create(name=ANNOTATOR)  # без прав на модели: работает через страницы

    perms = Permission.objects.filter(content_type__app_label__in=RESEARCH_APPS).select_related(
        "content_type"
    )
    researcher.permissions.set(
        p
        for p in perms
        if (p.content_type.app_label, p.content_type.model) not in READ_ONLY_MODELS
        or p.codename.startswith("view_")
    )
    observer.permissions.set(
        p
        for p in perms
        if p.codename.startswith("view_")
        and (p.content_type.app_label, p.content_type.model) not in READ_ONLY_MODELS
    )


def assign_role(user, role: str) -> None:
    """Назначает одну роль. Исследователю нужен вход в админку, пока панель не готова (Этап 9)."""
    groups = Group.objects.filter(name__in=ROLES.values())
    user.groups.remove(*groups)
    user.groups.add(Group.objects.get(name=ROLES[role]))
    user.is_staff = role == "researcher" or user.is_superuser
    user.save(update_fields=["is_staff"])
    user.__dict__.pop("_role_names", None)


class RoleRequiredMixin(AccessMixin):
    """Аноним → на вход; вошедший без нужной роли → 403."""

    role_check = staticmethod(can_view_dashboard)

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not self.role_check(request.user):
            self.raise_exception = True
            return self.handle_no_permission()
        return super().dispatch(request, *args, **kwargs)


class ViewerRequiredMixin(RoleRequiredMixin):
    """Рабочая часть: наблюдатель или исследователь."""

    role_check = staticmethod(can_view_dashboard)


class ResearcherRequiredMixin(RoleRequiredMixin):
    """Панель исследователя: только исследователь."""

    role_check = staticmethod(is_researcher)


class AnnotatorRequiredMixin(RoleRequiredMixin):
    """Разметка валидационной выборки: разметчик или исследователь."""

    role_check = staticmethod(can_annotate)
