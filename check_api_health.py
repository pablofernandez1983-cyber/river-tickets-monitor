"""
Chequeo de salud, 1 vez por dia: renderiza con un browser headless la pagina
real de noticias de entradas (la que ve un humano) y compara los titulos que
aparecen ahi contra lo que trae la API que usa monitor_river_entradas.py.

Si hay una noticia de categoria "Entradas" visible en la pagina que la API NO
esta devolviendo, es señal de que River cambio algo (la API, el nombre de la
categoria, etc.) y el monitor principal podria estar fallando en silencio ->
manda un mail de alerta. Si todo coincide, no manda nada.
"""
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText

import requests
from playwright.sync_api import sync_playwright

API_LIST = "https://www.riverplate.com/api/v1/news/published"
PAGE_URL = "https://www.riverplate.com/noticias/entradas/"
CATEGORY_SLUG = "entradas"
CATEGORY_ID = 21  # ver monitor_river_entradas.py
CATEGORY_TAG = "ENTRADAS"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
# lineas de navegacion/menu que aparecen pegadas a la palabra "ENTRADAS" en la
# pagina (nav bar, filtros) y no son titulos de noticias reales
RUIDO_CONOCIDO = {"tienda", "femenino", "masculino", "socios", "museo"}


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        print(f"ERROR: falta la variable de entorno {name}.", file=sys.stderr)
        sys.exit(1)
    return value


GMAIL_USER = _require_env("GMAIL_USER")
GMAIL_APP_PASSWORD = _require_env("GMAIL_APP_PASSWORD")
NOTIFY_TO = "pablofernandez1983@gmail.com"


def normalizar(texto: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", texto.lower())


def titulos_de_la_api() -> set:
    resp = requests.get(
        API_LIST,
        headers=HEADERS,
        params={"context": "general", "limit": 30, "categoria": CATEGORY_ID},
        timeout=20,
    )
    resp.raise_for_status()
    entradas = resp.json().get("data", [])
    entradas = [it for it in entradas if any(c.get("slug") == CATEGORY_SLUG for c in it.get("categories", []))]
    return {normalizar(it["title"]) for it in entradas}


def titulos_de_la_pagina() -> list:
    """Renderiza la pagina real y extrae los titulos de las tarjetas de
    categoria ENTRADAS, aprovechando el patron de texto que se ve al leer la
    pagina (fecha / ENTRADAS / titulo, en lineas consecutivas)."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        page.goto(PAGE_URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(3000)
        texto = page.inner_text("body")
        browser.close()

    lineas = [l.strip() for l in texto.split("\n") if l.strip()]
    titulos = []
    for i, linea in enumerate(lineas):
        if linea.upper() == CATEGORY_TAG and i + 1 < len(lineas):
            titulos.append(lineas[i + 1])
    return titulos


def send_email(subject: str, body: str) -> None:
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = NOTIFY_TO
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, NOTIFY_TO, msg.as_string())


def main() -> None:
    try:
        api_titulos = titulos_de_la_api()
    except Exception as e:
        print(f"ERROR consultando la API: {e}")
        send_email(
            "⚠️ Monitor de entradas River: la API no responde",
            f"El chequeo diario de salud no pudo consultar la API de noticias.\n\n"
            f"Error: {e}\n\nRevisar si River cambio algo en riverplate.com/api/v1/news/published.",
        )
        return

    try:
        page_titulos = titulos_de_la_pagina()
    except Exception as e:
        print(f"ERROR renderizando la pagina: {e}")
        # no manda mail: puede ser un problema transitorio del browser/CI,
        # no necesariamente algo roto en River. Se reintenta mañana.
        return

    faltantes = [
        t for t in page_titulos
        if normalizar(t) not in api_titulos and normalizar(t) not in RUIDO_CONOCIDO and len(t) > 8
    ]

    if not faltantes:
        print(f"OK: los {len(page_titulos)} titulos de la pagina coinciden con la API.")
        return

    print(f"MISMATCH: {faltantes}")
    send_email(
        "⚠️ Monitor de entradas River: posible cambio en la API",
        "El chequeo diario encontró noticias de categoría Entradas en la página real "
        "que la API NO está devolviendo — posible señal de que River cambió algo "
        "(el monitor principal podría estar fallando en silencio):\n\n"
        + "\n".join(f"- {t}" for t in faltantes)
        + f"\n\nPágina revisada: {PAGE_URL}\nAPI revisada: {API_LIST}",
    )


if __name__ == "__main__":
    main()
    sys.exit(0)
