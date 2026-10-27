# -*- coding: utf-8 -*-
"""Чьи данные на экране — видно, и вендорский переход на установку клиента не бывает тихим.

Сервер фильтрует сторы по арендатору и умеет вендорский переход (`X-ABOP-Tenant` для admin/support,
с записью в журнал). Десктоп об этом не знал ничего: человек видел адрес установки и своё имя — но
не видел, ЧЬИ данные перед ним. Для поддержки, зашедшей посмотреть установку заказчика, это
то же самое, что уже исправлено во вложениях чата: экран ничем не отличается от обычной работы.

Два правила, и оба проверяются здесь:

1. Арендатора берём У СЕРВЕРА. Правило вычисления (`tenant` → `org` → `organization`) живёт на
   сервере; местная копия разошлась бы с ним молча — ровно так уже случилось с уровнем доступа,
   когда интерфейс рисовал кнопку, на которую не было прав.
2. Показываем то, что ДЕЙСТВУЕТ, а не то, что попросили. Переход сервер вправе не пустить, и тогда
   десктоп обязан сказать «остались в своём», иначе он врёт в самом опасном месте.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
DESKTOP = КОРЕНЬ / "desktop"
sys.path.insert(0, str(DESKTOP))

APP_JS = (DESKTOP / "ui" / "core" / "app.js").read_text(encoding="utf-8")
INDEX = (DESKTOP / "ui" / "index.html").read_text(encoding="utf-8")
КЛИЕНТ = (DESKTOP / "sidecar" / "abop_client.py").read_text(encoding="utf-8")


@pytest.fixture()
def сайдкар():
    sc = pytest.importorskip("sidecar.app", reason="сайдкар десктопа недоступен")
    from sidecar import abop_client, auth
    было = (abop_client.me, auth.token, auth.claims, abop_client.просим_арендатора())
    auth.token = lambda: "t"
    auth.claims = lambda: {"sub": "u-1", "preferred_username": "support",
                           "realm_access": {"roles": ["support"]}}
    yield sc, abop_client
    abop_client.me, auth.token, auth.claims = было[0], было[1], было[2]
    abop_client.смотреть_у(было[3])


def _сервер(действует: str, свой: str, уровень: str = "support"):
    """Ответ `/api/me` настоящего сервера: он и решает, какой арендатор действует."""
    return lambda: {"user": {"tenant": свой, "tenant_own": свой,
                             "tenant_active": действует, "level": уровень}}


# ── просьба о переходе уходит заголовком ─────────────────────────────────────────────────────────

def test_заголовок_перехода_только_когда_просят(сайдкар):
    _, abop = сайдкар
    abop.смотреть_у("")
    assert "X-ABOP-Tenant" not in abop._заголовки(), (
        "заголовок перехода уходит всегда — каждый запрос выглядел бы как вендорский переход")
    abop.смотреть_у("  ACME  ")
    h = abop._заголовки()
    assert h["X-ABOP-Tenant"] == "acme", "имя арендатора не приведено к одному виду"
    assert h["Authorization"].startswith("Bearer "), "токен пользователя потерян"


def test_просьба_не_живёт_на_диске():
    """«Смотрю у клиента» не должно переживать перезапуск молча: это состояние, а не настройка."""
    assert "_ПРОСИМ_АРЕНДАТОРА" in КЛИЕНТ
    i = КЛИЕНТ.index("_ПРОСИМ_АРЕНДАТОРА: str")
    блок = КЛИЕНТ[max(0, i - 600):i]
    assert "памяти процесса" in блок, "не сказано, где живёт просьба и почему"
    for запрет in ("config.save", "json.dump", "open("):
        assert запрет not in КЛИЕНТ[i:i + 1200], f"просьба о переходе пишется на диск ({запрет})"


def test_заголовки_собираются_в_одном_месте():
    """Второе место сборки заголовков — это запрос, который тихо уйдёт БЕЗ перехода."""
    assert КЛИЕНТ.count('"Content-Type": "application/json"') == 1, (
        "заголовки собираются в нескольких местах — один из путей потеряет арендатора")
    assert КЛИЕНТ.count("_заголовки()") >= 2, "не все вызовы берут общие заголовки"


# ── арендатор приходит от сервера ────────────────────────────────────────────────────────────────

def test_арендатор_со_слов_сервера(сайдкар):
    sc, abop = сайдкар
    from fastapi.testclient import TestClient
    abop.me = _сервер("acme", "vendor")
    r = TestClient(sc.app).get("/api/auth/me").json()
    assert r["tenant"] == "acme", "показан не тот арендатор, который действует на сервере"
    assert r["tenant_own"] == "vendor"
    assert r["tenant_known"] is True
    assert r["tenant_can_switch"] is True, "поддержке вендора не показан переключатель"


def test_без_связи_арендатор_неизвестен(сайдкар):
    """Показать «свой» наугад значит обещать, что на экране данные вашей организации."""
    sc, abop = сайдкар
    from fastapi.testclient import TestClient

    def boom():
        raise abop.AbopError(0, "ConnectionRefused")

    abop.me = boom
    r = TestClient(sc.app).get("/api/auth/me").json()
    assert r["tenant_known"] is False
    assert r["tenant"] == "" and r["tenant_own"] == "", (
        "при недоступном сервере арендатор угадан — это выдача незнания за знание")
    assert r["tenant_can_switch"] is False


def test_не_вендору_переключатель_не_предлагается(сайдкар):
    sc, abop = сайдкар
    from fastapi.testclient import TestClient
    abop.me = _сервер("acme", "acme", уровень="manager")
    r = TestClient(sc.app).get("/api/auth/me").json()
    assert r["tenant_can_switch"] is False


# ── переход показывает то, что действует ─────────────────────────────────────────────────────────

def test_переход_отдаёт_действующего_а_не_запрошенного(сайдкар):
    """Сервер оставил своего арендатора — десктоп обязан сказать это, а не «смотрю у клиента»."""
    sc, abop = сайдкар
    from fastapi.testclient import TestClient
    abop.me = _сервер("vendor", "vendor")       # сервер переход НЕ пустил
    r = TestClient(sc.app).post("/api/auth/tenant", json={"tenant": "acme"}).json()
    assert r["tenant"] == "vendor", "десктоп показал просьбу как факт"
    assert r["tenant_asked"] == "acme", "не видно, о чём просили — отказ нечем объяснить"


def test_возврат_к_своим_сбрасывает_просьбу(сайдкар):
    sc, abop = сайдкар
    from fastapi.testclient import TestClient
    abop.me = _сервер("acme", "vendor")
    c = TestClient(sc.app)
    c.post("/api/auth/tenant", json={"tenant": "acme"})
    assert abop.просим_арендатора() == "acme"
    abop.me = _сервер("vendor", "vendor")
    r = c.post("/api/auth/tenant", json={"tenant": ""}).json()
    assert abop.просим_арендатора() == ""
    assert r["tenant"] == "vendor"


# ── вложения клиента не смешиваются со своими ────────────────────────────────────────────────────

def test_область_чата_учитывает_переход(сайдкар):
    """Та же дыра, что уже закрыта по установкам: номера чатов везде начинаются с единицы."""
    _, abop = сайдкар
    from sidecar import config
    from sidecar.modules.chat import store
    config.ABOP = "http://abop.vendor.ru:8091"
    abop.смотреть_у("")
    своё = store.область()
    abop.смотреть_у("acme")
    клиент = store.область()
    assert своё != клиент, "вложения клиента попадут в чаты своей организации"
    assert клиент.endswith(":acme"), клиент
    assert клиент.startswith(своё), (
        "своя работа должна остаться в прежней области — иначе понадобился бы перенос")


# ── интерфейс ────────────────────────────────────────────────────────────────────────────────────

def test_плашка_видна_только_на_чужих_данных():
    """Плашка у всех — шум; плашка никогда — та самая незаметность, из-за которой это и опасно."""
    assert 'id="tenantPill"' in INDEX and "hidden" in INDEX[INDEX.index('id="tenantPill"'):][:200]
    тело = APP_JS[APP_JS.index("function renderTenant"):APP_JS.index("function tenantDetails")]
    assert "ctx.tenant !== ctx.tenantOwn" in тело, "плашка не отличает чужие данные от своих"
    assert "pill.hidden = !чужое" in тело
    assert "ctx.tenantKnown" in тело, (
        "при неизвестном арендаторе плашка молчит — а молчание читается как «данные свои»")


def test_карточка_связи_называет_арендатора():
    тело = APP_JS[APP_JS.index("function linkDetails"):APP_JS.index("function markOffline")]
    assert '["Арендатор"' in тело, "в карточке подключения не сказано, чьи данные на экране"
    assert "неизвестно" in тело, "при молчании сервера карточка не говорит о незнании"
    assert "данные клиента, не ваши" in тело


def test_переход_записан_в_журнал_и_об_этом_сказано():
    тело = APP_JS[APP_JS.index("function tenantDetails"):APP_JS.index("function renderLink")]
    assert "журнал" in тело, "человеку не сказано, что переход фиксируется"
    assert "ctx.tenantCanSwitch" in тело, "переключатель показан всем"
    # Сам переход вынесен из окна в общую функцию (её зовут и плашка в шапке, и это окно):
    # отказ сервера показывается там, где переход и выполняется.
    переход = APP_JS[APP_JS.index("async function перейтиКАрендатору("):][:1200]
    assert "переход не разрешён" in переход, "отказ сервера не показывается человеку"


def test_учётная_запись_показывает_арендатора():
    i = APP_JS.index('const rows = [["имя", nm]')
    блок = APP_JS[i:i + 700]
    assert '"арендатор"' in блок
    assert "данные клиента" in блок
