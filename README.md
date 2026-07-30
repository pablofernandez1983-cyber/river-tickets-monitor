# river-tickets-monitor

Chequea cada hora si River Plate publicó una noticia nueva de categoría
"Entradas" (via la API pública `riverplate.com/api/v1/news/published`, no la
web — es una SPA sin contenido server-rendered). Si encuentra la fecha/hora de
venta para la categoría de socio "Socio sin lugar en el Monumental", carga 4
recordatorios en Fosfovita (Supabase, tabla `recordatorios_app`): inmediato,
1 día antes, 1 hora antes y 5 minutos antes de que abra la venta. También
manda un mail de aviso a pablofernandez1983@gmail.com.

Corre por GitHub Actions (`.github/workflows/check.yml`, cron cada hora) y
también localmente por Tarea Programada de Windows (`RiverEntradasCheck`) como
respaldo — el estado (qué noticias ya se procesaron) se comparte en la tabla
`automation_state` de Supabase, así que no se duplican alarmas entre ambos.

Requiere los secrets/env vars: `SUPABASE_URL`, `SUPABASE_ANON_KEY`,
`GMAIL_USER`, `GMAIL_APP_PASSWORD`.
