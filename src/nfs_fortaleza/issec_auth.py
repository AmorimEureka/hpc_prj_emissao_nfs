"""Login humano no portal ISSEC pelo modal de captcha do Receita Certa.

O portal exige um captcha de imagem. A DAG abre o login no desktop remoto,
publica o desafio e aguarda um usuário resolvê-lo; a sessão resultante segue
para a extração. O captcha nunca é respondido automaticamente.
"""

from __future__ import annotations

import logging
import time
from contextlib import suppress
from typing import Mapping
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import (
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from nfs_fortaleza.issec_portal import (
    LOGIN_PATH,
    IssecPortalClient,
    IssecPortalError,
)
from nfs_fortaleza.spu_auth import (
    SpuInteractiveAuthError,
    _complete_recaptcha_challenge,
    _ensure_visible_browser_available,
    _find_browser_executable,
    _publish_recaptcha_challenge,
)


LOGGER = logging.getLogger(__name__)
USERNAME_SELECTOR = "#P9999_USERNAME"
PASSWORD_SELECTOR = "#P9999_PASSWORD"
SESSION_SELECTOR = "#pInstance"
PAGE_TIMEOUT_MS = 60_000


class IssecInteractiveAuthError(IssecPortalError):
    """O login humano no portal ISSEC não foi concluído."""


def authenticate_with_human_captcha(
    client: IssecPortalClient,
    *,
    timeout_seconds: float = 1800,
    challenge_metadata: Mapping[str, object] | None = None,
) -> None:
    """Aguarda o login humano e entrega a sessão do navegador ao cliente."""
    challenge_id: str | None = None
    playwright = None
    browser = None
    try:
        _ensure_visible_browser_available()
        playwright = sync_playwright().start()
        launch_options: dict[str, object] = {
            "headless": False,
            "args": [
                "--kiosk",
                "--start-maximized",
                "--window-position=0,0",
                "--window-size=1600,900",
                "--no-xshm",
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        executable = _find_browser_executable()
        if executable:
            launch_options["executable_path"] = executable
        browser = playwright.chromium.launch(**launch_options)
        context = browser.new_context(
            no_viewport=True,
            locale="pt-BR",
            timezone_id="America/Fortaleza",
        )
        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)
        page.goto(
            urljoin(client.base_url, LOGIN_PATH),
            wait_until="domcontentloaded",
        )
        page.fill(USERNAME_SELECTOR, client.username)
        page.fill(PASSWORD_SELECTOR, client.password)
        challenge_id = _publish_recaptcha_challenge(
            timeout_seconds=timeout_seconds,
            metadata=challenge_metadata,
        )
        page.bring_to_front()
        LOGGER.warning(
            "Login do portal ISSEC aberto. Um usuário precisa resolver o "
            "captcha no modal do Receita Certa e clicar em Acessar."
        )
        session_id = _wait_for_human_login(
            page,
            timeout_seconds=timeout_seconds,
        )
        client.adopt_session(context.cookies(), session_id)
        LOGGER.info("Sessão do portal ISSEC recebida do login humano.")
    except IssecInteractiveAuthError:
        raise
    except (
        PlaywrightError,
        PlaywrightTimeoutError,
        SpuInteractiveAuthError,
    ) as exc:
        raise IssecInteractiveAuthError(
            "Não foi possível concluir o login humano no portal ISSEC."
        ) from exc
    finally:
        if challenge_id is not None:
            with suppress(SpuInteractiveAuthError):
                _complete_recaptcha_challenge(challenge_id)
        if browser is not None:
            with suppress(PlaywrightError):
                browser.close()
        if playwright is not None:
            playwright.stop()


def _wait_for_human_login(page: Page, *, timeout_seconds: float) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if page.is_closed():
            raise IssecInteractiveAuthError(
                "A janela do portal ISSEC foi fechada antes do login."
            )
        try:
            session_id = _logged_in_session(page)
            if session_id:
                return session_id
            page.wait_for_timeout(500)
        except PlaywrightError:
            # A página pode estar navegando; só o fechamento encerra a espera.
            if page.is_closed():
                raise IssecInteractiveAuthError(
                    "A janela do portal ISSEC foi fechada antes do login."
                ) from None
    raise IssecInteractiveAuthError(
        "Tempo esgotado aguardando o login humano no portal ISSEC."
    )


def _logged_in_session(page: Page) -> str:
    if "/home" not in urlsplit(page.url).path:
        return ""
    if page.locator(USERNAME_SELECTOR).count():
        return ""
    if not page.locator(SESSION_SELECTOR).count():
        return ""
    return page.locator(SESSION_SELECTOR).input_value()
