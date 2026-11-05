# Bee

App web (Flask + SQLite) para pedidos en tiendas del campus.

## Ejecutar en local

```bash
pip install -r requirements.txt
python app.py
```

Variable opcional: `SECRET_KEY` (clave de sesión; configúrala en producción).

Demo tienda: `cafeteria@bee.test` / `bee123`

## Despliegue

Incluye `Procfile` (`gunicorn app:app`) para Render, Railway, Heroku, etc.
