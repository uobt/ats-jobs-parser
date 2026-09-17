# ats-jobs-parser

Единый парсер вакансий с публичных job-board API систем найма: компании,
 вакансии и **сигналы найма** (дельта «появилась/закрылась») в SQLite.

## Покрытие

**12 ATS-адаптеров** (без ключей, все эндпоинты публичные):

Greenhouse · Lever · Ashby · Workable · SmartRecruiters · Recruitee ·
Breezy · Teamtailor · BambooHR · Personio · Rippling · Workday

**6 лент-агрегаторов** (выдача целиком): Arbeitnow · Himalayas · RemoteOK ·
Remotive · Jobicy · The Muse

## Установка

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m unittest tests.test_jobs     # приёмка: 55 тестов OK
```

## Использование

```bash
python jobs/run.py run --limit 200     # обход компаний (прерываемый/продолжаемый)
python jobs/run.py feeds --pages 3     # ленты-агрегаторы
python jobs/run.py signals --days 30   # компании с сигналом найма
python jobs/run.py export --out out.csv
python jobs/run.py status              # база и здоровье адаптеров
```

Реестр компаний пополняется тремя путями: дискавери из URL вакансий
(`jobs/discover.py` — 12 шаблонов площадок), импорт снимка реестра
(`import-snapshot`, read-only) или ручные пары `(source, slug)`.

## Ключевые свойства

- **Дельта, а не снимок**: закрытие вакансии считается только по успешно
  опрошенному ATS; ленты никогда не закрывают (скользящее окно).
- **Честные даты**: Lever — epoch в миллисекундах, Workable — дата без
  времени, Workday — «Posted 30+ Days Ago» → диапазон; нераспознанная дата
  не подставляется «сегодня».
- **Транспорт**: троттлинг на хост, ретраи с backoff, уважение Retry-After;
  ошибка одной компании не роняет прогон; прокси не используются.
- **Домены компаний**: ATS-хосты (greenhouse.io и т.п.) отбрасываются,
  домен из текста вакансии берётся только при совпадении с названием.

## Структура

```
jobs/
  sources.py    12 адаптеров ATS: один источник — один класс
  feeds.py      6 лент: выдача целиком, БЕЗ закрытия вакансий
  discover.py   URL вакансии → (площадка, slug компании)
  fetch.py      HTTP: троттлинг, ретраи, бэкофф
  normalize.py  единая схема Vacancy, даты/гео/сеньорити
  store.py      SQLite: компании, вакансии, дельты, прогоны
  bridge.py     вакансии → сигналы найма со скорингом
  run.py        CLI
lib/            config (yaml+dotenv), console (UTF-8), signals, portal
```

## Правила

- Парсер работает на собственной SQLite (`data/jobs/jobs.db`) и не пишет
  в чужие базы. Портал-снимок — только read-only сессия с проверкой counts.
- Ключи площадок не нужны; публичные эндпоинты опрашиваются вежливо
  (1 req/s на хост).

## Лицензия

MIT — см. [LICENSE](LICENSE).
