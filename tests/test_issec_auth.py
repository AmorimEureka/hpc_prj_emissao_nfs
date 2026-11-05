from __future__ import annotations

import json

import pytest

from nfs_fortaleza import issec_auth
from nfs_fortaleza.issec_auth import (
    IssecInteractiveAuthError,
    _wait_for_human_login,
    authenticate_with_human_captcha,
)
from nfs_fortaleza.issec_portal import IssecPortalClient


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    def count(self):
        if self.selector == issec_auth.USERNAME_SELECTOR:
            return 0 if self.page.logged_in else 1
        return 1

    def input_value(self):
        return "998877"


class FakePage:
    def __init__(self, ticks_until_login=1, closed=False):
        self.url = "http://portal/ords/r/aplicacoes/portal-credenciado/login"
        self.logged_in = False
        self.ticks = ticks_until_login
        self.closed = closed
        self.filled = {}

    def set_default_timeout(self, _value):
        return None

    def goto(self, url, **_kwargs):
        self.goto_url = url

    def fill(self, selector, value):
        self.filled[selector] = value

    def bring_to_front(self):
        return None

    def is_closed(self):
        return self.closed

    def locator(self, selector):
        return FakeLocator(self, selector)

    def wait_for_timeout(self, _value):
        self.ticks -= 1
        if self.ticks <= 0:
            self.logged_in = True
            self.url = "http://portal/ords/r/aplicacoes/portal-credenciado/home"


class FakeContext:
    def __init__(self, page):
        self.page = page

    def new_page(self):
        return self.page

    def cookies(self):
        return [{"name": "ORA_WWV_APP_112", "value": "abc", "path": "/"}]


class FakeBrowser:
    def __init__(self, page):
        self.page = page
        self.closed = False

    def new_context(self, **_kwargs):
        return FakeContext(self.page)

    def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self, browser):
        self.chromium = self
        self.browser = browser
        self.stopped = False
        self.launch_options = None

    def launch(self, **options):
        self.launch_options = options
        return self.browser

    def start(self):
        return self

    def stop(self):
        self.stopped = True


@pytest.fixture
def portal(monkeypatch, tmp_path):
    status = tmp_path / "status.json"
    monkeypatch.setenv("SPU_RECAPTCHA_STATUS_PATH", str(status))
    monkeypatch.setenv("DISPLAY", ":99")
    page = FakePage()
    playwright = FakePlaywright(FakeBrowser(page))
    monkeypatch.setattr(issec_auth, "sync_playwright", lambda: playwright)
    monkeypatch.setattr(issec_auth, "_find_browser_executable", lambda: None)
    return page, playwright, status


def _client():
    return IssecPortalClient("1234567", "segredo", base_url="http://portal/ords/")


def test_login_humano_publica_desafio_e_entrega_sessao(portal, monkeypatch):
    page, playwright, status = portal
    published = {}
    real_wait = issec_auth._wait_for_human_login

    def wait(page_, **kwargs):
        published.update(json.loads(status.read_text(encoding="utf-8")))
        return real_wait(page_, **kwargs)

    monkeypatch.setattr(issec_auth, "_wait_for_human_login", wait)
    client = _client()

    authenticate_with_human_captcha(
        client,
        timeout_seconds=30,
        challenge_metadata={
            "dag_id": "extracao_processos_portal_issec",
            "run_id": "manual",
            "task_id": "extrair_e_carregar",
        },
    )

    assert published["active"] is True
    assert published["dag_id"] == "extracao_processos_portal_issec"
    assert "segredo" not in json.dumps(published)
    assert page.filled == {
        "#P9999_USERNAME": "1234567",
        "#P9999_PASSWORD": "segredo",
    }
    assert playwright.launch_options["headless"] is False
    assert client.session_id == "998877"
    assert client.session.cookies.get("ORA_WWV_APP_112") == "abc"
    assert json.loads(status.read_text(encoding="utf-8"))["active"] is False
    assert playwright.browser.closed and playwright.stopped


def test_captcha_nao_e_respondido_pela_automacao(portal):
    page, _playwright, _status = portal

    authenticate_with_human_captcha(_client(), timeout_seconds=30)

    assert not any("CAPTCHA" in selector for selector in page.filled)


def test_tempo_esgotado_encerra_desafio_e_navegador(portal, monkeypatch):
    page, playwright, status = portal
    page.ticks = 10**9
    monkeypatch.setattr(
        issec_auth,
        "_wait_for_human_login",
        lambda *_a, **_k: (_ for _ in ()).throw(
            IssecInteractiveAuthError("Tempo esgotado")
        ),
    )

    with pytest.raises(IssecInteractiveAuthError):
        authenticate_with_human_captcha(_client(), timeout_seconds=1)

    assert json.loads(status.read_text(encoding="utf-8"))["active"] is False
    assert playwright.browser.closed and playwright.stopped


def test_erro_de_login_mantem_espera_na_tela_de_login():
    page = FakePage(ticks_until_login=10**9)

    with pytest.raises(IssecInteractiveAuthError, match="Tempo esgotado"):
        _wait_for_human_login(page, timeout_seconds=0.01)


def test_janela_fechada_interrompe_a_espera():
    with pytest.raises(IssecInteractiveAuthError, match="fechada"):
        _wait_for_human_login(FakePage(closed=True), timeout_seconds=5)
