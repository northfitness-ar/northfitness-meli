# Diagnóstico de memoria

`GET /healthz` expone un bloque `runtime` sin credenciales, URLs, payloads ni nombres de tareas:

- RSS actual y variación desde la construcción de la aplicación.
- tiempo activo y cantidad total/pendiente de tareas `asyncio`.
- total, fallas, concurrencia actual y pico de solicitudes a Mercado Libre.

Esto permite correlacionar un reinicio de Render con memoria, tareas o concurrencia sin habilitar
logs de autorización. El contador de tareas incluye las tareas internas del servidor y de MCP; no
crece por cada llamada a Mercado Libre.

## Investigación del crecimiento

La ruta de Mercado Libre construía y destruía un `httpx.AsyncClient` completo en cada GET, PUT y
POST. Los ciclos del monitor y de recuperación hacen muchas llamadas paginadas, por lo que también
se recreaban pools, transportes TLS y recursos de conexión repetidamente. Ahora toda la aplicación
comparte un cliente acotado a 20 conexiones (10 keep-alive), y lo cierra durante el apagado del
lifespan. Los encabezados de autorización siguen siendo específicos de cada request y nunca se
guardan en las métricas.

La reproducción local ejecuta 10.000 consultas simuladas en 20 ciclos. Se debe observar una sola
tarea durante todos los ciclos, cero solicitudes activas al terminar y crecimiento acotado luego
de la inicialización del pool. La prueba automatizada agrega además un límite al crecimiento neto
del heap trazado para 1.000 consultas.

## Alcance e incertidumbre

- MCP ya se ejecuta con `stateless_http=True`, por lo que no conserva una sesión en memoria por
  request. Las sesiones privadas del monitor y sus snapshots residen en SQLite, no en una caché RAM.
- No se modificaron los intervalos ni las tareas de atención, reclamos o monitor, ni las reglas de
  cálculo financiero.
- La reproducción usa un transporte HTTP simulado para no enviar credenciales ni depender de APIs
  externas. Antes de considerar cerrado el incidente, observar `runtime` en Render durante un plazo
  mayor a los 12–14 minutos reportados. Si RSS aumenta mientras tareas y solicitudes activas quedan
  estables, tomar un perfil en producción: podría existir retención adicional en FastMCP/Authlib o
  en el allocator nativo que este repositorio no puede reproducir sin el patrón real de tráfico.
