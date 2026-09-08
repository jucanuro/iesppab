```sh
python -m venv venv
source venv/Scripts/activate
python -m pip install --upgrade pip; pip install -r requirements.txt

# Windows necesita la DLL libmagic. Instala el binario
pip install python-magic-bin==0.4.14

# .env.local minimal
DJANGO_ENV=local
SECRET_KEY=dev-cambia-esto-por-algo-aleatorio
DEBUG=True
ALLOWED_HOSTS=127.0.0.1,localhost
CSRF_TRUSTED_ORIGINS=http://127.0.0.1:8000,http://localhost:8000
DB_ENGINE=django.db.backends.sqlite3
WEB_ANALYSIS_ENABLED=False
OAI_HARVEST_ENABLED=False

# redis
docker run -d --name redis -p 6379:6379 redis

# Migraciones y superusuario superuser123 superuser123@gmail.com P45s_123
python manage.py migrate
python manage.py createsuperuser

# Verifica desde PowerShell (Windows) que el puerto llega:
python -c "import socket; s=socket.create_connection(('127.0.0.1',6379),3); print('OK'); s.close()"
```

# Run

```sh
docker start redis

# levanta el Worker (en PowerShell, con el venv activado)
.\venv\Scripts\Activate.ps1
celery -A config worker -l info -P solo

# levanta runserver
source venv/Scripts/activate
python manage.py runserver

# http://127.0.0.1:8000/
superuser123 
superuser123@gmail.com 
P45s_123
```

## Para trabajar necesitas 3 cosas corriendo

1. **Redis** (WSL): `docker start redis` si el contenedor ya existe
2. **Worker Celery** (PowerShell + venv): `celery -A config worker -l info -P solo`
3. **Web** (otra PowerShell + venv): `python manage.py runserver`

## Probar el flujo

1. Entra a http://127.0.0.1:8000, inicia sesión
2. Asegúrate de tener una **Institution** activa en `/admin/` (si aún no la creaste)
3. Sube un documento en `/documentos/subir/` y dale a analizar
4. En la terminal del worker verás `Task apps.analysis.tasks.run_document_analysis[...] received` → `succeeded`
5. El reporte aparece en `/documentos/<uuid>/reporte/`

## Pendiente (opcional)

- **Certificados PDF**: cuando llegues a esa parte necesitarás el _GTK3 runtime_ para WeasyPrint.
- **Similitud web / OAI**: lo dejaste desactivado en `.env.local` (`WEB_ANALYSIS_ENABLED=False`, `OAI_HARVEST_ENABLED=False`). Para activarlo necesitas una `BRAVE_SEARCH_API_KEY` real.
