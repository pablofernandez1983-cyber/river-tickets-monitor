"""
Carga en Fosfovita (Supabase, tabla recordatorios_app) los recordatorios de cada
partido de River (todas las competencias, fixture de ESPN):
  - notificacion el dia del partido (09:00, o 3 horas antes si juega muy temprano)
  - alarma 5 minutos antes del inicio

Solo carga partidos con horario confirmado (ESPN timeValid) dentro de los proximos
VENTANA_DIAS. Si un partido se reprograma, borra (borrado logico) los recordatorios
viejos y carga los nuevos; si desaparece del fixture, los borra.

Estado compartido en Supabase (automation_state, key river_partidos) igual que el
monitor de entradas, asi que se puede correr desde GitHub Actions y local sin duplicar.
"""
import sys
from datetime import datetime, timedelta

import requests

from monitor_river_entradas import (
    ARG_TZ, SUPABASE_URL, SUPABASE_USER, _supabase_headers, log, notify_app_sync,
)

ESPN_SCHEDULE = "https://site.api.espn.com/apis/site/v2/sports/soccer/all/teams/16/schedule"
RIVER_ID = "16"
STATE_KEY = "river_partidos"
VENTANA_DIAS = 10
HORA_AVISO = 9  # hora del aviso del dia del partido


def load_state() -> dict:
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/automation_state",
        headers=_supabase_headers(),
        params={"key": f"eq.{STATE_KEY}", "select": "value"},
        timeout=20,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0]["value"] if rows else {}


def save_state(state: dict) -> None:
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/automation_state",
        headers={**_supabase_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
        json={"key": STATE_KEY, "value": state, "updated_at": datetime.now(tz=ARG_TZ).isoformat()},
        timeout=20,
    )
    resp.raise_for_status()


def proximos_partidos(ahora: datetime) -> dict:
    """{event_id: {...}} de los partidos con horario confirmado en la ventana."""
    resp = requests.get(ESPN_SCHEDULE, params={"fixture": "true"}, timeout=20)
    resp.raise_for_status()
    partidos = {}
    for ev in resp.json().get("events", []):
        comp = ev["competitions"][0]
        if comp["status"]["type"]["state"] != "pre" or not ev.get("timeValid", comp.get("timeValid")):
            continue
        inicio = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(ARG_TZ)
        if not (ahora < inicio <= ahora + timedelta(days=VENTANA_DIAS)):
            continue
        river = next(c for c in comp["competitors"] if c["team"]["id"] == RIVER_ID)
        rival = next(c for c in comp["competitors"] if c["team"]["id"] != RIVER_ID)
        partidos[ev["id"]] = {
            "inicio": inicio.isoformat(),
            "rival": rival["team"]["displayName"],
            "local": river.get("homeAway") == "home",
            "estadio": (comp.get("venue") or {}).get("fullName", ""),
            "torneo": (ev.get("league") or {}).get("name", ""),
        }
    return partidos


def insertar(p: dict, ahora: datetime) -> list:
    inicio = datetime.fromisoformat(p["inicio"])
    hora = inicio.strftime("%H:%M")
    condicion = "de local" if p["local"] else "de visitante"
    aviso = inicio.replace(hour=HORA_AVISO, minute=0)
    if aviso > inicio - timedelta(hours=3):
        aviso = inicio - timedelta(hours=3)
    aviso = max(aviso, ahora)  # si se detecto tarde, avisar ya
    detalle = " · ".join(x for x in (p["torneo"], p["estadio"]) if x)
    filas = [
        {
            "texto": f"⚽ Hoy juega River vs. {p['rival']} a las {hora} hs ({condicion})"
                     + (f" - {detalle}" if detalle else ""),
            "scheduled_at": aviso.isoformat(),
            "tipo": "notificacion",
        },
        {
            "texto": f"⚽ En 5 minutos empieza River vs. {p['rival']} ({hora} hs).",
            "scheduled_at": (inicio - timedelta(minutes=5)).isoformat(),
            "tipo": "alarma",
        },
    ]
    for f in filas:
        f.update({"usuario": SUPABASE_USER, "completado": False, "borrado": False, "sonado": False})
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/recordatorios_app",
        headers={**_supabase_headers(), "Prefer": "return=representation"},
        json=filas,
        timeout=20,
    )
    resp.raise_for_status()
    return [r["id"] for r in resp.json()]


def borrar(ids: list) -> None:
    if not ids:
        return
    resp = requests.patch(
        f"{SUPABASE_URL}/rest/v1/recordatorios_app",
        headers=_supabase_headers(),
        params={"id": f"in.({','.join(str(i) for i in ids)})", "sonado": "eq.false"},
        json={"borrado": True},
        timeout=20,
    )
    resp.raise_for_status()


def main() -> None:
    ahora = datetime.now(tz=ARG_TZ)
    try:
        partidos = proximos_partidos(ahora)
        estado = load_state()
    except Exception as e:
        log(f"[partidos] ERROR trayendo fixture/estado: {e}")
        return

    cambios = False
    nuevo_estado = {}
    for eid, guardado in estado.items():
        if eid in partidos and partidos[eid]["inicio"] == guardado["inicio"]:
            nuevo_estado[eid] = guardado
        elif datetime.fromisoformat(guardado["inicio"]) > ahora:
            # reprogramado o sacado del fixture: los recordatorios viejos ya no sirven
            borrar(guardado["ids"])
            cambios = True
            log(f"[partidos] Borrados recordatorios de {guardado.get('rival')} ({guardado['inicio']}).")
        # si ya se jugo, simplemente se olvida

    for eid, p in partidos.items():
        if eid in nuevo_estado:
            continue
        ids = insertar(p, ahora)
        nuevo_estado[eid] = {"inicio": p["inicio"], "rival": p["rival"], "ids": ids}
        cambios = True
        log(f"[partidos] Cargados recordatorios: River vs. {p['rival']} {p['inicio']}.")

    if cambios:
        notify_app_sync()
    else:
        log(f"[partidos] Sin cambios ({len(partidos)} partidos en los proximos {VENTANA_DIAS} dias).")
    save_state(nuevo_estado)


if __name__ == "__main__":
    main()
    sys.exit(0)
