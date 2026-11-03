# TrollTrace — pravidla vývoje

## Jazyk

- **Celé rozhraní je v češtině**: administrace (verbose_name, choices, help_text, nadpisy),
  veřejná část a panel (šablony), upozornění v panelu a v Telegramu, zprávy a chyby, které
  uživatel uvidí (včetně textů ukládaných do DB, např. důvody zařazení), výstup management
  příkazů a demo data. `LANGUAGE_CODE = "cs"`.
- Rusky jsou pouze pokyny autora projektu (komunikace s asistentem). Ruské komentáře a
  docstringy v kódu a README zatím zůstávají; nové texty rozhraní nikdy nepsat rusky.
- Nové uživatelské texty psát rovnou česky se správnou diakritikou a českými uvozovkami „…“.
- Změna popisků modelů vytváří migraci (jen metadata, bez změny schématu) — přiložit ji.

## Další konvence

- Jedna etapa = jedna větev `stage-N-…`, commit obsahuje „Etapa N“ / „Этап N“.
- Průběh etap se zaznamenává na Trello nástěnce „TrollTrace — разработка (диплом)“.
- Před commitem: `ruff format . && ruff check .` a `pytest` (v kontejneru `web`).
