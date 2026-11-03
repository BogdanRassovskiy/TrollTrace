# Списки источников

Источники подключаются командой (идемпотентно — повторный запуск обновляет, не дублирует):

```bash
docker compose exec web python manage.py import_sources apps/ingestion/sources/mainstream.json
docker compose exec web python manage.py import_sources apps/ingestion/sources/attributed.json
docker compose exec web python manage.py import_psp_deputies   # слой E: депутаты + источник стенограмм
```

| Файл | Слой | Статус |
|---|---|---|
| `mainstream.json` | D | ČT24, iROZHLAS, Seznam Zprávy, iDNES, Novinky, Blesk, ČTK (ленты проверены 2026-11-02, ČTK — 2026-11-03) |
| `attributed.json` | A | Pravda Česko (VIGINUM, DFRLab), 42TCen (BIS VZ 2024) — проверено 2026-11-03 |
| — | B, C | списки альтернативных сайтов и Telegram-каналов составляет исследователь |

## Формат

```json
{
  "sources": [
    {
      "name": "Название сайта",
      "url": "https://example.cz/",
      "layer": "B",
      "platform": "web",
      "collect_interval_minutes": 60,
      "request_delay_seconds": 3,
      "external_rating": "…", "external_rating_source": "konspiratori.sk",
      "feeds": [
        {"url": "https://example.cz/rss", "rubric": "Domácí"},
        {"url": "https://example.cz/sitemap-news.xml", "kind": "sitemap"}
      ],
      "actor": {"name": "Название сайта", "type": "media", "criterion": "Основание включения"}
    },
    {
      "name": "Telegram (публичные каналы)",
      "url": "https://t.me/",
      "layer": "C",
      "platform": "telegram",
      "collect_interval_minutes": 60,
      "channels": [
        {"handle": "nazev_kanalu", "actor": {"name": "Название канала", "criterion": "…"}},
        {"handle": "kanal_politika", "actor": {"name": "Имя политика", "type": "politician", "party": "…", "is_hybrid": true}}
      ]
    }
  ]
}
```

- **Слой A** (атрибутированные сети): `layer: "A"`, `"actor": {"type": "attributed_network", …}`, плюс
  `attribution_by` и `attribution_url` — ссылка на внешний отчёт (собственной атрибуции нет, README §1).
- Ленты (`feeds[].kind`): `rss` (по умолчанию), `sitemap` (в т. ч. индекс — читаются
  `max_child_sitemaps` свежих), `listing` — HTML-страница со списком статей для сайтов без
  пригодной ленты; ссылки отбираются по `collector_config.listing_link_pattern` (regex пути).
- `collector_config`:
  - `rubrics_in_scope` / `rubrics_out_of_scope` — начала рубрик (без регистра и диакритики)
    для `Document.in_scope`; без списков в рамках всё;
  - `max_articles` — новых статей за запуск (100);
  - `origin_xpath` — XPath к адресу первоисточника на странице → `meta.origin_urls`.
- `expected_language: ""` — язык не проверяется (сайт на нескольких языках).
- Сайты: `actor` — сайт целиком как один актор. Telegram: один источник, по актору на канал.
- Telegram собирается из публичного веб-превью `t.me/s/<канал>`: без логина и API-ключей, только
  открытые каналы. Канал с выключенным превью попадёт в ошибки запуска сбора.
