"""Хранилище статики для разработки: к адресу файла добавляется ?v=<время изменения>.

Без collectstatic у статики в разработке нет хэша в имени, и браузер может держать старый JS
при новом HTML, даже не спрашивая сервер. Версия в адресе меняется вместе с файлом, поэтому
старая копия из кэша просто не используется. В продакшене хэш даёт ManifestStaticFilesStorage.
"""

import os

from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import StaticFilesStorage


class VersionedStaticFilesStorage(StaticFilesStorage):
    def url(self, name):
        url = super().url(name)
        path = finders.find(name) if name else None
        if isinstance(path, str):
            url = f"{url}?v={int(os.path.getmtime(path))}"
        return url
