# Списки источников

Источники подключаются командой (идемпотентно — повторный запуск обновляет, не дублирует):

```bash
docker compose exec web python manage.py import_sources apps/ingestion/sources/mainstream.json
docker compose exec web python manage.py import_sources apps/ingestion/sources/attributed.json
docker compose exec web python manage.py import_sources apps/ingestion/sources/alternative.json
docker compose exec web python manage.py import_sources apps/ingestion/sources/russian.json
docker compose exec web python manage.py import_sources apps/ingestion/sources/security.json && docker compose exec web python manage.py import_institutions
docker compose exec web python manage.py import_psp_deputies   # слой E: депутаты + источник стенограмм
```

| Файл | Слой | Статус |
|---|---|---|
| `mainstream.json` | D | ČT24, iROZHLAS, Seznam Zprávy, iDNES, Novinky, Blesk, ČTK (ленты проверены 2026-11-02, ČTK — 2026-11-03) |
| `attributed.json` | A | Pravda Česko (VIGINUM, DFRLab), 42TCen (BIS VZ 2024) — проверено 2026-11-03 |
| `security.json` | I | BIS (výpis aktualit), NÚKIB (RSS), MV (výpis zpravodajství) — проверено 2026-11-03; органы — `import_institutions` |
| `alternative.json` | B | ParlamentníListy.cz (согласие с cookies в `collector_config.cookies`), AC24, Protiproud, Nová republika, Svobodný svět, InfoKurýr, Tadesco, První zprávy (список исследователя, проверено 2026-11-06) |
| `russian.json` | Z | RIA Novosti (рубрики «В мире», «Политика»), RT на русском (russiatoday.ru: «В мире», «Бывший СССР», «Россия», «Мнения»); ссылки без `utm_` — robots.txt RT запрещает адреса с «?» — проверено 2026-11-06 |
| — | C | список Telegram-каналов составляет исследователь |

## Формат

```json
{
  "sources": [
    {
      "name": "Название сайта",
      "url": "https://example.cz/",
      "layer": "B",
      "platform": "web",
      "parser": "nazev-webu",
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

- **`parser` обязателен для веб-источника** (этап 3.5): ключ парсера из `apps/ingestion/parsers/`.
  Без него `import_sources` источник не подключает. Новый сайт: сначала модуль парсера и
  фикстуры (`tests/fixtures/parsers/<ключ>/`), потом запись здесь. Вёрстку (тело, дата, автор,
  первоисточник, ссылки на странице-списке) задаёт только парсер, не `collector_config`.
- **Слой A** (атрибутированные сети): `layer: "A"`, `"actor": {"type": "attributed_network", …}`, плюс
  `attribution_by` и `attribution_url` — ссылка на внешний отчёт (собственной атрибуции нет, README §1).
- Ленты (`feeds[].kind`): `rss` (по умолчанию), `sitemap` (в т. ч. индекс — читаются
  `max_child_sitemaps` свежих), `listing` — HTML-страница со списком статей для сайтов без
  пригодной ленты; ссылки отбираются по `listing_pattern` / `listing_xpath` парсера сайта.
- `collector_config`:
  - `rubrics_in_scope` / `rubrics_out_of_scope` — начала рубрик (без регистра и диакритики)
    для `Document.in_scope`; без списков в рамках всё;
  - `max_articles` — новых статей за запуск (100);
  - `max_date_lag_days` — допустимое отставание даты от сбора (30; у органов 730);
  - `cookies` — `{имя: значение}`, отправляются только на домен источника (например, записанное
    согласие с cookies сайта, у которого без него вместо статьи «стена» согласия).
- `expected_language: ""` — язык не проверяется (сайт на нескольких языках).
- Сайты: `actor` — сайт целиком как один актор. Telegram: один источник, по актору на канал.
- Telegram собирается из публичного веб-превью `t.me/s/<канал>`: без логина и API-ключей, только
  открытые каналы. Канал с выключенным превью попадёт в ошибки запуска сбора.
