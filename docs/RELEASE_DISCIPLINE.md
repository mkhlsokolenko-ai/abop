# Дисциплина релизов и правок (Блок 5, 2026-09-26)

## Веб (`webapp/`)

- Исходники фронта — `webapp/src/` (`template.html`, `components/*.dc.html`, `vendor/`, `fonts/`, `manifest.json`, `shell.html`). `webapp/index.html` — **сборочный артефакт**, руками не править.
- Правка → `python webapp/build.py build` (lint через `node --check` + сборка) → коммит и `src/`, и `index.html`.
- CI (`.github/workflows/ci.yml`, job `smoke`) падает, если `index.html` расходится с `src/` (`build.py check`) или JS не парсится.
- Если бандл правили в обход (старые патч-скрипты), один раз выполнить `python webapp/build.py extract`, чтобы вернуть исходники в актуальное состояние.
- Раскатка на стенд: `tar … | ssh … docker build` (см. память `server-deploy-access`); `docker cp` — только для горячей проверки, теряется при пересоздании контейнера.

## Smoke перед раскаткой

- `python -m pytest -q tests/test_smoke_api.py` — API in-process без Postgres/Keycloak (health, бандл, /api/me, агенты, флот, очередь, находки/метрики пилота, нормы, нормализатор находок, очередь в памяти).
- `python tests/smoke_web.py` при поднятом сервере — страница рендерится, ориентиры/ARIA на месте, навигация и линзы работают, ошибок JS нет.
- Оба шага выполняет CI на каждый push/PR.

## Десктоп (`desktop/`)

- Любая правка `desktop/sidecar`, `desktop/electron`, `desktop/ui` → поднять `version` в `desktop/package.json` **и** `desktop/sidecar/config.py` (fallback-версия) в том же коммите.
- Релиз собирает `desktop-release.yml` по тегу `desktop-vX.Y.Z`; **тег ставит владелец** (явное слово), не ассистент.
- CI job `desktop-guard`: если `desktop/` менялся после последнего тега, а версия в `package.json` равна версии тега → ошибка; если версия поднята, а тега нет → предупреждение «релиз не собран».
- UI по «Пути А» (`/desktop-ui-bundle`) приезжает с сервера без пересборки .exe; сайдкар и Electron — только через релиз.

## Текущее состояние

- `desktop/package.json` = **1.0.7**, последний тег `desktop-v1.0.6`; с 1.0.6 менялся сайдкар (очередь/цепочки) → нужен тег `desktop-v1.0.7` по слову владельца.
- Временные файлы корня (`_ctx_*`, `_patch_*`, `_dump*` …, 334 шт.) перенесены в `_attic/` (gitignored). В корне остались только `_1c_gen.py`, `_1c_src/`, `_deck_tmp/` — рабочие активы.
