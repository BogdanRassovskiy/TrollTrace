"""Настройка reuse-source-layers заменена на reuse-layers (этап 3.8).

Заимствования ищутся между всеми слоями, направление определяет время публикации, а не
заранее выбранный «слой-источник». Старая строка убирается, чтобы панель не показывала
настройку, которая ни на что не влияет; история изменений остаётся в журнале аудита.
"""

from django.db import migrations


def remove(apps, schema_editor):
    apps.get_model("panel", "Setting").objects.filter(key="reuse-source-layers").delete()


class Migration(migrations.Migration):
    dependencies = [("panel", "0003_czech_labels")]

    operations = [migrations.RunPython(remove, migrations.RunPython.noop)]
