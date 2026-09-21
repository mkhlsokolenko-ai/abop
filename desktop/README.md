# APE Desktop — модульная оболочка курса

Десктоп-приложение (скачал → установил) поверх курсового шлюза. **Электрон = тонкое окно**,
**Python-сайдкар = движок** (локальный FastAPI). Всё общение окна с движком — по HTTP на
`127.0.0.1`, поэтому **окно можно заменить** (Electron → Tauri/pywebview) не трогая движок.

## Архитектура (слои)

```
┌─────────────────────────────────────────────┐
│ Electron shell (electron/main.js)            │  спавнит сайдкар, рендерит ui/
│  └─ BrowserWindow → ui/index.html            │
├─────────────────────────────────────────────┤
│ UI (модульный фронт, ui/)                    │
│  core/app.js  — загрузчик модулей + API      │  тема в core/theme.css (свапается)
│  modules/<id>/panel.js — панель фичи          │
├───────────────── HTTP 127.0.0.1 ─────────────┤
│ Python sidecar (sidecar/) — FastAPI движок   │
│  app.py — ядро: auth + реестр модулей        │
│  registry.py — авто-обнаружение modules/*    │
│  auth.py — loopback-PKCE вход (GitHub)        │
│  gateway.py — вызовы MCP-шлюза (chat/rag)     │
│  db.py — локальный SQLite (треды/сообщения)   │
│  modules/<id>/module.py — MANIFEST + router  │
└─────────────────────────────────────────────┘
         │ chat / rag_index / rag_search (JWT)
         ▼  курсовой шлюз: MCP → self-host Qwen 30B / DeepSeek / S3 / Qdrant
```

**Модульность (главное требование):** новая фича = новый модуль, ядро не трогаем.
- **Backend-модуль:** папка `sidecar/modules/<id>/` с `module.py`, где есть `MANIFEST` (id/title/icon/ui/order)
  и `router` (FastAPI). Реестр монтирует его на `/api/modules/<id>`.
- **Frontend-модуль:** `ui/modules/<ui>/panel.js` с `export async function mount(root, ctx)`.
  Ядро берёт список из `/api/modules` и динамически импортирует панель.
- **Тема** — только `ui/core/theme.css` (CSS-переменные). Дизайн меняется без правки логики.

## Модули сейчас
- **chat** — треды/темы + история (локальный SQLite), профиль (standard 30B / code / ask),
  скиллы из UI (подмешиваются в system), вложения → RAG (`rag_index`/`rag_search`),
  память треда (последние реплики в контексте), мультиагенты (передача задачи по ролям
  ресёрчер → аналитик → критик).

## Запуск (dev)
Требуется: **Node 18+** (Electron) и **Python 3.10+**.
```bash
cd desktop
py -m pip install -r sidecar/requirements.txt   # движок: fastapi, uvicorn, pydantic
npm install                                      # electron, electron-builder
npm start                                        # Electron сам поднимет сайдкар и окно
```
Сайдкар можно гонять и отдельно (для отладки без окна):
```bash
cd desktop && APE_SIDECAR_PORT=8799 py -m sidecar.app
# затем открыть http://127.0.0.1:8799/api/health , /api/modules
```
Данные приложения (токены, треды): `~/.ape-desktop/` (переопределяется `APE_DESKTOP_HOME`).

## Сборка инсталлятора (Windows) — Python пользователю НЕ нужен
Два шага: сначала бинарь сайдкара (PyInstaller), потом инсталлятор (electron-builder).
```bash
cd desktop
py -m pip install pyinstaller
py -m PyInstaller --noconfirm --clean build-sidecar.spec   # → dist/ape-sidecar/ (onedir + _internal)
CSC_IDENTITY_AUTO_DISCOVERY=false npm run dist             # → dist/APE Desktop Setup x.y.z.exe (~108 МБ)
```
Как это устроено:
- `build-sidecar.spec` собирает `run_sidecar.py` (→ `sidecar.app:main`) в автономный `ape-sidecar.exe`
  (collect_all uvicorn/fastapi/pydantic + submodules `sidecar.modules`; реестр имеет fallback-список
  модулей — во frozen-сборке `pkgutil` может не перечислить пакеты).
- `package.json → build.extraResources` кладёт `dist/ape-sidecar` в `resources/ape-sidecar`;
  `main.js` в проде (`app.isPackaged`) запускает бинарь оттуда, в dev — `py -m sidecar.app`.

**Гейты сборки (важно на Windows без прав администратора):**
- `signAndEditExecutable: false` + `CSC_IDENTITY_AUTO_DISCOVERY=false` — иначе electron-builder тянет
  `winCodeSign`, распаковка которого содержит mac-симлинки и падает («Cannot create symbolic link»)
  без Developer Mode/админа. Мы не подписываем сборку.
- Инсталлятор **без подписи** → при первом запуске Windows SmartScreen покажет предупреждение
  («Подробнее → Выполнить в любом случае»). Для тихой установки позже добавить code-signing сертификат.

## Авто-доставка обновлений (без ручной переустановки)

Гибрид, чтобы не переставлять приложение на каждое изменение:

- **Бэкенд** (шлюз/роутинг/стриминг/модель) — деплой на сервер, клиентам реинсталл **не нужен**.
- **UI** — **удалённый**: если задан `APE_UI_URL`, `main.js` грузит фронт с сервера (правки без релиза),
  с фоллбеком на локальную копию из asar при офлайне. Пусто → локальный UI. Хост UI — GitHub Pages
  или портал (любой URL, отдающий `ui/`).
- **Сайдкар + оболочка** — **electron-updater** (тихий авто-апдейт с GitHub Releases): при запуске
  проверяет версию, докачивает, ставит на перезапуск. Конфиг `build.publish` (github owner/repo) в
  `package.json`; проверка — в `main.js` (`checkUpdates()`, только `app.isPackaged`).

**CI (`.github/workflows/desktop-release.yml`):** пуш тега `desktop-vX.Y.Z` → windows-раннер собирает
сайдкар (PyInstaller) + инсталлятор (electron-builder) и **публикует в GitHub Releases** (`GITHUB_TOKEN`).
Клиенты подхватывают апдейт сами.

Релиз вручную (локально): `set GH_TOKEN=... && npm run publish` (или через CI по тегу).
Первый релиз нужно опубликовать один раз — дальше авто-апдейт от него отсчитывается.
Сборка без code-sign → SmartScreen предупредит при первой установке (для тихой — сертификат позже).

## Roadmap (модулями, без переписывания)
1. **ocr-tesseract** — модуль OCR (pytesseract + бинарь Tesseract в extraResources); панель «распознать документ» → текст → RAG.
2. **ml-модули** — мини-нейронки под менеджерские задачи (классификация писем/заявок, приоритизация, извлечение сущностей) на onnxruntime/sklearn, локально.
3. **abop** — смычка с ABOP: `gateway.py`/модули реализуют контракты ABOP (Agent/Tool/Data-плоскости); ABOP становится движком, оболочка — витриной.
4. Упаковка сайдкара (PyInstaller) + автообновление (electron-updater).
