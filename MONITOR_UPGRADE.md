# Monitor: gestión y comparación automática

Versión anterior a estas mejoras: `29ae1ba0c24ab78e4500dd7579e62475e76c8b01`.
Para revertir, revertir el commit de esta mejora conservando los cambios posteriores.
No restaurar configuraciones privadas ni sobrescribir otros cambios de main.

- La facturación mostrada mantiene el criterio solicitado: ventas menos cancelaciones.
- Las compensaciones de operaciones canceladas no se infieren como ventas a partir del estado del pago. El balance puede registrar esos ingresos con documentación propia.
- Ganancia de productos: suma completa de ganancias luego de comisiones, mercadería y reintegros registrados. Un costo desconocido deja el total sin disponibilidad.
- Resultado de gestión: conserva la política configurada, con Ads según días cerrados, impuestos estimados y logística registrada. No incorpora IVA mensual ni gastos extraordinarios del balance.
- Ticket promedio: facturación de ventas pagadas dividida por cantidad de ventas pagadas. Unidades por venta usa la misma población.
- Comparación superior: semana anterior al mismo corte; mensual contra el mes anterior, con la misma cantidad de días y hora. En meses más cortos se usa el último día común.
- Lecturas de ventas: histórico reutilizado por hasta cinco minutos; cola del día abierto por treinta segundos. Los cortes históricos por hora se filtran sobre ventas del día completo, sin atribuirles visitas intradiarias inexistentes.
- Actualizar fuerza una lectura nueva de ventas. La caché de visitas y logística conserva sus plazos propios.
- Las selecciones de vista, fecha, referencia y orden de productos se guardan localmente, sin credenciales ni datos comerciales.

Verificación: `python -m pytest -q` y `node test_monitor_ui.cjs`.
