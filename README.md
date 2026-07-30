# river-tickets-monitor

Chequea cada hora si River Plate publicó una noticia nueva de categoría
"Entradas" (via la API pública `riverplate.com/api/v1/news/published`, no la
web — es una SPA sin contenido server-rendered). Si encuentra la fecha/hora de
venta para la categoría de socio "Socio sin lugar en el Monumental", carga 4
recordatorios en Fosfovita (Supabase, tabla `recordatorios_app`): inmediato,
1 día antes, 1 hora antes y 5 minutos antes de que abra la venta. También
manda un mail de aviso a pablofernandez1983@gmail.com.

Corre por GitHub Actions (`.github/workflows/check.yml`, cron cada 5 min —
repo público, minutos de Actions gratis) y también localmente por Tarea
Programada de Windows (`RiverEntradasCheck`, cada 1 hora) como respaldo — el
estado (qué noticias ya se procesaron) se comparte en la tabla
`automation_state` de Supabase, así que no se duplican alarmas entre ambos.

La API de noticias (`/api/v1/news/published`) es interna del sitio, no está
documentada públicamente — se encontró por ingeniería inversa del bundle JS
del frontend. Soporta un parámetro `categoria` (numérico, 21 = Entradas) que
filtra del lado del servidor, mucho más confiable que traer las últimas ~20
noticias generales y filtrar por categoría del lado del cliente.

**Chequeo de salud diario** (`check_api_health.py`,
`.github/workflows/health_check.yml`): renderiza con un browser headless la
página real de noticias de entradas y compara los títulos visibles contra lo
que devuelve la API. Si hay un desfasaje, manda un mail de alerta — es la
red de seguridad por si River cambia la API sin avisar y el monitor principal
empieza a fallar en silencio.

Requiere los secrets/env vars: `SUPABASE_URL`, `SUPABASE_ANON_KEY`,
`GMAIL_USER`, `GMAIL_APP_PASSWORD`.
