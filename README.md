# Nexo POS

Base sencilla de punto de venta con Flask y SQLite.

## Arranque

En Ubuntu instala primero el soporte para entornos virtuales si `python3 -m venv .venv` indica que `ensurepip` no está disponible:

```bash
sudo apt update
sudo apt install python3.14-venv
```

Después ejecuta:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

No uses `pip3 install` directamente sobre el Python del sistema. Si no puedes instalar `python3.14-venv`, puedes usar como alternativa `pip3 install --break-system-packages -r requirements.txt`, aunque el entorno virtual es la opción recomendada.

Abre `http://127.0.0.1:5000`. El primer usuario es `admin` con contraseña `admin123`. Cambia la clave y `SECRET_KEY` antes de usarlo en producción.

## Incluye

- Login y roles `admin`, `admin_almacen` y `vendedor`.
- Base para asignar usuarios a tiendas y almacenes mediante `user_stores` y `user_warehouses`.
- Alta de tiendas, almacenes y productos.
- Consulta de existencias por cada tienda o almacén.
- Entradas y salidas rápidas con motivo e historial de movimientos.
- Traspaso de existencias con validación de stock y asignaciones.
- Registro de ventas por tienda y método de pago.
- Cierre diario único por tienda con desglose contable.

Es una base inicial: para producción conviene añadir gestión de usuarios/asignaciones desde UI, detalle de líneas de venta, CSRF, migraciones, auditoría y contraseñas con Werkzeug/Argon2.
