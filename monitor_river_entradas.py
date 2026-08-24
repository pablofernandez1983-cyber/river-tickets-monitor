"""
Chequea si River Plate publicó una noticia nueva de categoria "Entradas"
(https://www.riverplate.com/api/v1/news/published) y, si encuentra la fecha/hora
de venta para la categoria "Socio sin lugar en el Monumental", carga 4
recordatorios en Fosfovita (Supabase, tabla recordatorios_app):
  - inmediato (recien salio la noticia)
  - 1 dia antes de la venta
  - 1 hora antes
  - 5 minutos antes
Tambien manda un mail de aviso. Corre via Tarea Programada de Windows cada hora
Y via GitHub Actions cada hora (lo que se ejecute primero gana, el estado se
comparte en Supabase asi que no se duplica).

El sitio de River es una SPA (React) sin contenido en el HTML crudo, por eso se
usa la API JSON que consume el propio frontend en vez de scrapear la pagina.

Credenciales: se leen SIEMPRE de variables de entorno (SUPABASE_URL,
SUPABASE_ANON_KEY, GMAIL_USER, GMAIL_APP_PASSWORD) — nunca hardcodeadas aca,
porque este archivo vive en un repo de GitHub. En GitHub Actions vienen de
Secrets; localmente las setea run_check.bat antes de invocar python.
"""
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

import requests

BASE_DIR = os.path.dirname(__file__)
LOG_PATH = os.path.join(BASE_DIR, "check.log")

API_LIST = "https://www.riverplate.com/api/v1/news/published"
API_DETAIL = "https://www.riverplate.com/api/v1/news/{slug}"
CATEGORY_SLUG = "entradas"
CATEGORY_ID = 21  # id numerico de la categoria "Entradas" (visto en categoria.id de /news/{slug})
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        print(f"ERROR: falta la variable de entorno {name}.", file=sys.stderr)
        sys.exit(1)
    return value


SUPABASE_URL = _require_env("SUPABASE_URL")
SUPABASE_ANON_KEY = _require_env("SUPABASE_ANON_KEY")
SUPABASE_USER = "pablo"
STATE_KEY = "river_tickets_entradas"

GMAIL_USER = _require_env("GMAIL_USER")
GMAIL_APP_PASSWORD = _require_env("GMAIL_APP_PASSWORD")
NOTIFY_TO = "pablofernandez1983@gmail.com"

# Opcionales: si estan seteadas, avisan a la app Fosfovita Alarma por push (FCM) que hay
# recordatorios nuevos para que reprograme las alarmas sin esperar a que se abra la app.
FOSFOVITA_ALARMA_BACKEND_URL = os.environ.get("FOSFOVITA_ALARMA_BACKEND_URL", "")
FOSFOVITA_ALARMA_API_KEY = os.environ.get("FOSFOVITA_ALARMA_API_KEY", "")

CATEGORIA_PABLO = "Socio sin lugar en el Monumental"
HEADING_MARKER = "SOCIOS SIN TU LUGAR"

ARG_TZ = timezone(timedelta(hours=-3))

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
DATE_RE = re.compile(
    r"(\d{1,2})\s+de\s+(" + "|".join(MESES.keys()) + r")"
    r"(?:[^.\n]{0,20}?a las\s+(\d{1,2})(?::(\d{2}))?)?",
    re.IGNORECASE,
)


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _supabase_headers() -> dict:
    return {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        "Content-Type": "application/json",
    }


def load_state() -> dict:
    """Estado compartido en Supabase (tabla automation_state) para que el chequeo
    local (Task Scheduler) y el de GitHub Actions no dupliquen alarmas."""
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/automation_state",
            headers=_supabase_headers(),
            params={"key": f"eq.{STATE_KEY}", "select": "value"},
            timeout=20,
        )
        resp.raise_for_status()
        rows = resp.json()
        if rows:
            return {item["id"]: item for item in rows[0]["value"]}
    except Exception as e:
        log(f"ERROR leyendo estado de Supabase (se asume vacio): {e}")
    return {}


def save_state(items: list) -> None:
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/automation_state",
        headers={**_supabase_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
        json={"key": STATE_KEY, "value": items, "updated_at": datetime.now(tz=ARG_TZ).isoformat()},
        timeout=20,
    )
    resp.raise_for_status()


def extract_text_blocks(content_json_str: str) -> str:
    """Recorre el arbol Craft.js (ROOT -> nodes) y concatena el texto de los TextBlock, en orden."""
    tree = json.loads(content_json_str)
    parts = []

    def visit(node_id: str, seen: set) -> None:
        if node_id in seen or node_id not in tree:
            return
        seen.add(node_id)
        node = tree[node_id]
        node_type = node.get("type", {})
        resolved = node_type.get("resolvedName") if isinstance(node_type, dict) else node_type
        if resolved == "TextBlock":
            text = node.get("props", {}).get("text", "")
            if text:
                parts.append(text)
        for child_id in node.get("nodes", []):
            visit(child_id, seen)

    visit("ROOT", set())
    return "\n".join(parts)


def parse_sale_datetime(full_text: str, published_dt: datetime):
    """Busca la fecha/hora de venta para CATEGORIA_PABLO; si no encuentra esa
    categoria puntual, usa la primera fecha con hora que aparezca en el texto."""
    idx = full_text.upper().find(HEADING_MARKER)
    search_text = full_text[idx:idx + 400] if idx != -1 else full_text

    match = None
    for m in DATE_RE.finditer(search_text):
        if m.group(3):  # tiene hora -> es la que sirve para armar un recordatorio
            match = m
            break
    if not match and idx != -1:
        # la categoria existe pero no se pudo leer una hora puntual: no inventar
        return None
    if not match:
        for m in DATE_RE.finditer(full_text):
            if m.group(3):
                match = m
                break
    if not match:
        return None

    day = int(match.group(1))
    month = MESES[match.group(2).lower()]
    hour = int(match.group(3))
    minute = int(match.group(4) or 0)
    year = published_dt.year
    candidate = datetime(year, month, day, hour, minute, tzinfo=ARG_TZ)
    # si la fecha calculada quedara mas de 2 meses antes de la publicacion,
    # probablemente cruza de anio (ej. noticia de diciembre para venta de enero)
    if (published_dt.replace(tzinfo=ARG_TZ) - candidate).days > 60:
        candidate = candidate.replace(year=year + 1)
    return candidate


def extraer_rival(title: str) -> str:
    m = re.search(r"[Rr]iver\s*(?:vs\.?|v/s|-)\s*([A-ZÁÉÍÓÚÑ][\w. ]+?)(?:\s*\(|$)", title)
    return m.group(1).strip() if m else title


def insertar_recordatorios(titulo: str, rival: str, venta_dt: datetime) -> None:
    ahora = datetime.now(tz=ARG_TZ)
    hora_venta = venta_dt.strftime("%H:%M")
    filas = [
        {
            "texto": f"🎟️ Nueva noticia de entradas: {titulo}. Venta para vos "
                      f"({CATEGORIA_PABLO}): {venta_dt.strftime('%d/%m %H:%M')} hs.",
            "scheduled_at": ahora.isoformat(),
        },
        {
            "texto": f"⏰ Mañana {hora_venta} hs abre la venta de entradas River vs. "
                      f"{rival} para vos ({CATEGORIA_PABLO}) por RiverID.",
            "scheduled_at": (venta_dt - timedelta(days=1)).isoformat(),
        },
        {
            "texto": f"⏰ En 1 hora abre la venta de entradas River vs. {rival} para vos "
                      f"({CATEGORIA_PABLO}) - {hora_venta} hs por RiverID.",
            "scheduled_at": (venta_dt - timedelta(hours=1)).isoformat(),
        },
        {
            "texto": f"⏰ En 5 minutos abre la venta de entradas River vs. {rival} para vos "
                      f"({CATEGORIA_PABLO}) - {hora_venta} hs por RiverID.",
            "scheduled_at": (venta_dt - timedelta(minutes=5)).isoformat(),
        },
    ]
    for i, fila in enumerate(filas):
        # Solo la ultima (5 min antes) suena como alarma real (ignora silencio); las
        # primeras 3 son notificaciones normales para no saturar de alarmas al usuario.
        fila.update({
            "tipo": "alarma" if i == len(filas) - 1 else "notificacion",
            "usuario": SUPABASE_USER,
            "completado": False,
            "borrado": False,
            "sonado": False,
        })
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/recordatorios_app",
        headers={
            "apikey": SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
        json=filas,
        timeout=20,
    )
    resp.raise_for_status()
    log(f"Insertadas {len(filas)} filas en recordatorios_app para '{titulo}'.")


def notify_app_sync() -> None:
    """Avisa a la app Fosfovita Alarma (push FCM silencioso, sin sonido ni UI) que hay
    recordatorios nuevos para que reconcilie y reprograme alarmas en segundo plano — evita
    depender de que se abra la app para que las alarmas insertadas por SQL queden armadas.
    Best-effort: las filas ya quedaron en Supabase, asi que un fallo aca no debe cortar
    el resto del flujo (mail de aviso, guardado de estado)."""
    if not FOSFOVITA_ALARMA_BACKEND_URL or not FOSFOVITA_ALARMA_API_KEY:
        log("Aviso: FOSFOVITA_ALARMA_BACKEND_URL/API_KEY no configurados, no se notifica push a la app.")
        return
    try:
        resp = requests.post(
            f"{FOSFOVITA_ALARMA_BACKEND_URL}/notify_sync",
            headers={"X-API-Key": FOSFOVITA_ALARMA_API_KEY},
            json={"usuario": SUPABASE_USER},
            timeout=20,
        )
        resp.raise_for_status()
        log("Push de sync mandado a la app.")
    except Exception as e:
        log(f"ERROR mandando push de sync a la app (no bloqueante): {e}")


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
        resp = requests.get(
            API_LIST,
            headers=HEADERS,
            params={"context": "general", "limit": 20, "categoria": CATEGORY_ID},
            timeout=20,
        )
        resp.raise_for_status()
        entradas = resp.json().get("data", [])
    except Exception as e:
        log(f"ERROR trayendo listado de noticias: {e}")
        return

    # el filtro por categoria ya lo hace la API (parametro categoria=21); esto
    # es solo un resguardo por si algun item viniera mal categorizado
    entradas = [
        it for it in entradas
        if any(c.get("slug") == CATEGORY_SLUG for c in it.get("categories", []))
    ]

    estado_anterior = load_state()
    nuevos = [
        it for it in entradas
        if it["id"] not in estado_anterior
        or estado_anterior[it["id"]].get("updatedAt") != it.get("updatedAt")
    ]

    if not nuevos:
        log(f"Sin novedades ({len(entradas)} noticias de entradas revisadas).")
        save_state([{"id": it["id"], "slug": it["slug"], "updatedAt": it.get("updatedAt")} for it in entradas])
        return

    ahora = datetime.now(tz=ARG_TZ)
    resumen_mail = []
    for it in nuevos:
        titulo = it["title"]
        slug = it["slug"]
        log(f"Novedad detectada: {titulo} ({slug})")
        try:
            detail = requests.get(API_DETAIL.format(slug=slug), headers=HEADERS, timeout=20)
            detail.raise_for_status()
            content_json = detail.json()["data"]["contentJson"]
            texto = extract_text_blocks(content_json)
        except Exception as e:
            log(f"ERROR trayendo/parseando detalle de '{titulo}': {e}")
            resumen_mail.append(f"- {titulo}\n  No se pudo leer el detalle ({e}). Revisar a mano:\n  https://www.riverplate.com/noticias/entradas/{slug}")
            continue

        published_dt = datetime.strptime(it["publicationDate"], "%Y-%m-%d %H:%M:%S")
        venta_dt = parse_sale_datetime(texto, published_dt)
        rival = extraer_rival(titulo)

        published_dt_arg = published_dt.replace(tzinfo=ARG_TZ)

        if venta_dt and venta_dt < ahora:
            # el sitio a veces re-toca noticias viejas (updatedAt cambia) sin que la
            # venta siga vigente; no tiene sentido cargar recordatorios ya vencidos
            log(f"'{titulo}': venta ya vencida ({venta_dt:%d/%m/%Y %H:%M}), no se cargan recordatorios.")
            continue

        if not venta_dt and published_dt_arg < ahora - timedelta(days=3):
            # no se pudo leer la fecha de venta, pero la noticia en si es vieja
            # (re-tocada por el sitio) asi que tampoco vale la pena avisar por mail
            log(f"'{titulo}': no se pudo leer fecha de venta y la noticia es vieja (publicada {published_dt_arg:%d/%m/%Y}), se ignora.")
            continue

        if venta_dt:
            try:
                insertar_recordatorios(titulo, rival, venta_dt)
                notify_app_sync()
                resumen_mail.append(
                    f"- {titulo}\n  Venta para vos ({CATEGORIA_PABLO}): {venta_dt.strftime('%d/%m/%Y %H:%M')} hs.\n"
                    f"  Se cargaron 4 recordatorios en Fosfovita (inmediato, 1 dia, 1 hora y 5 min antes)."
                )
            except Exception as e:
                log(f"ERROR insertando en Supabase para '{titulo}': {e}")
                resumen_mail.append(f"- {titulo}\n  Encontrada, pero fallo la carga en Fosfovita ({e}). Revisar a mano.")
        else:
            log(f"No se pudo determinar fecha de venta para '{titulo}' — no se carga alarma.")
            resumen_mail.append(
                f"- {titulo}\n  No se pudo determinar con certeza la fecha/hora de venta para "
                f"'{CATEGORIA_PABLO}'. Revisar a mano:\n  https://www.riverplate.com/noticias/entradas/{slug}"
            )

    if resumen_mail:
        send_email(
            "🎟️ Nueva noticia de entradas River",
            "Se detectaron noticias nuevas de categoria Entradas en riverplate.com:\n\n"
            + "\n\n".join(resumen_mail),
        )
        log("Email de aviso enviado.")
    else:
        log("Todas las novedades eran ventas ya vencidas; no se manda mail.")

    save_state([{"id": it["id"], "slug": it["slug"], "updatedAt": it.get("updatedAt")} for it in entradas])


if __name__ == "__main__":
    main()
    sys.exit(0)
