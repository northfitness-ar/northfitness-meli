# Controles de campañas Mercado Ads

El conector conserva `nf_ads_presupuesto_fijar` y agrega:

- `nf_ads_estado_fijar`: activar (`active`) o pausar (`paused`). Exige estado,
  presupuesto y ROAS esperados obtenidos de una consulta reciente.
- `nf_ads_roas_fijar`: cambiar el ROAS objetivo expresado como múltiplo (5 = 5x),
  solo en estrategia `PROFITABILITY` y presupuesto manual.

Las herramientas requieren una orden explícita del titular. No agregan un worker
ni decisiones automáticas. Activar permite gasto al presupuesto vigente, que es un
promedio diario y no un tope rígido. La activación no modifica precio ni anuncios.

## Secuencia de uso

1. Consultar `nf_ads_campana` y confirmar campaña, presupuesto, estado y ROAS.
2. Ejecutar únicamente los ajustes que el usuario haya indicado con valores concretos.
3. Si hay ajustes previos a una activación, verificar cada uno antes de continuar.
4. Activar con la configuración recién consultada como valores esperados.
5. Informar el resultado observado, sin confundir respuesta HTTP aceptada con verificación.

Cada escritura envía un solo campo al endpoint de campañas existente y después relee
la campaña. Comprueba anunciante autorizado en Argentina, pertenencia de la campaña,
moneda ARS y conservación de las otras configuraciones. Pausar sigue permitido con
presupuesto automático; activar o cambiar presupuesto/ROAS en esa modalidad se bloquea.

## Reintentos y resultados inciertos

Usar el mismo `operation_id` para la misma operación. El registro SQLite reserva la
operación antes de escribir, con exclusión entre procesos. Los reintentos devuelven
el resultado persistido sin otro PUT. Una operación `unknown`, `accepted` sin
verificación o `verification_mismatch` bloquea cambios posteriores de esa campaña
hasta conciliación administrativa. No borrar el registro ni cambiar su estado sin
investigar el resultado remoto. Esta versión no incluye una acción de conciliación.

Los HTTP 5xx se tratan como inciertos; 401/403 requieren revisar permisos y no provocan
reintentos. La comprobación de valores esperados es local: Mercado Libre no recibe
una condición atómica, por lo que una edición externa simultánea sigue siendo posible.

## Validación y despliegue

Pruebas con API simulada: activación/pausa, ROAS, presupuesto, permisos, valores
obsoletos, modalidades no admitidas, persistencia de reintentos, errores HTTP,
verificación incompleta, conservación de campos y reservas entre instancias.

No se hicieron escrituras en campañas reales. La documentación pública de Mercado
Libre no pudo recuperarse durante esta implementación. El contrato de las nuevas
escrituras usa el endpoint v2 existente y los campos observados en la lectura;
su aceptación por la API real (especialmente `roas_target`) queda pendiente de
validación con una operación expresamente autorizada. No presentar pruebas simuladas
como validación de permisos o compatibilidad del servicio remoto.

Después de revisar y fusionar el PR, desplegar el servidor y actualizar/sincronizar
las herramientas del conector. Comprobar que aparecen ambos nombres nuevos. Hasta
entonces, esta conversación sigue disponiendo solo del ajuste de presupuesto.
No se requieren nuevas variables de entorno ni cambios de credenciales; si la API
responde 401/403, revisar los permisos de la integración existente.
