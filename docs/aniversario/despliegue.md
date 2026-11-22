# Aniversario — despliegue y retiro

Cada paso se ejecuta **solo con OK del Gerente TI**. Verificar el prompt `ubuntu@ip-172-31-40-70` antes de cada bloque.
Los valores entre `<>` se completan con la salida de la auditoría del servidor (Nginx/systemd actuales).

## Prueba local

```bash
conda deactivate; source venv/bin/activate
# .env.local: ANIVERSARIO_HOST=aniversario.localhost y agregarlo a ALLOWED_HOSTS
python manage.py migrate aniversario
python manage.py loaddata demo          # concurso + 7 + 7 candidatos de la maqueta, sin fotos
python manage.py runserver
# Abrir http://aniversario.localhost:8000/ (Chrome/Firefox resuelven *.localhost a 127.0.0.1)
# Originalidad sigue en http://127.0.0.1:8000/
DB_ENGINE=django.db.backends.sqlite3 python manage.py test apps.aniversario
```

## 1. DNS

Pedir al administrador del dominio: registro **A** `aniversario.iesp-alfonsobarranteslingan.edu.pe` → `3.143.125.27`
(la IP pública del EC2; confirmar con `curl -s ifconfig.me` en el servidor, o la IP elástica en la consola de AWS).
Verificar: `dig +short aniversario.iesp-alfonsobarranteslingan.edu.pe`.

## 2. Respaldo

```bash
sudo mkdir -p /var/backups/iesppab
sudo -u postgres pg_dump -Fc esp_alfonso_db | sudo tee /var/backups/iesppab/pre_aniversario_$(date +%Y%m%d_%H%M).dump > /dev/null
ls -lh /var/backups/iesppab/
```

## 3. Código

```bash
cd /var/www/projects/iesppab && git pull origin main
source venv/bin/activate
python manage.py migrate aniversario      # solo crea 3 tablas nuevas
python manage.py collectstatic --noinput
python manage.py check --deploy
```

Sin dependencias nuevas (Pillow y redis ya están en `requirements.txt`).

## 4. Nginx y HTTPS (script)

Cada subdominio sirve su propia app desde el mismo Gunicorn: Nginx pasa el `Host` y
`HostUrlconfMiddleware` elige el urlconf. El bloque de originalidad no se toca.

```bash
sudo bash deploy/aniversario/instalar_nginx.sh
```

El script (idempotente, se puede repetir):

1. Detecta el `proxy_pass` que ya usa originalidad en `/etc/nginx/sites-enabled/` (si no, `ANIV_UPSTREAM=...`).
2. Respalda en `/root/nginx_backups/` y escribe `/etc/nginx/sites-available/aniversario` +
   `/etc/nginx/aniversario_proxy.conf`: `/static/`, **solo** `/media/aniversario/` (el resto de `/media/` → 404,
   ahí están las tesis privadas) y `limit_req` 30/min por IP (ráfaga 30) en `/votar/confirmar/`, holgado por el CGNAT de datos móviles.
3. Crea `media/aniversario/` con el dueño de `media/`.
4. `nginx -t`; si falla, restaura y no recarga.
5. Pide el certificado con `certbot certonly --webroot` (no edita el archivo), regenera el bloque con 443 y
   redirección 80→443, y deja `renew_hook = systemctl reload nginx` para la renovación.
6. Comprueba `/` 200, `/admin/` 404 y `/media/private/` 404.

Opcional en el bloque de originalidad (videos desde el admin): `client_max_body_size 110m` (videos de hasta 100 MB). Ver el valor actual con
`sudo grep -rn client_max_body_size /etc/nginx/`. Antes de subir 14 videos, revisar `df -h /` (14 × 100 MB ≈ 1,4 GB).

## 5. Variables de entorno

No hace falta tocar el `.env`. Con `DJANGO_ENV=production` el código ya:

- usa `ANIVERSARIO_HOST=aniversario.iesp-alfonsobarranteslingan.edu.pe`;
- lo agrega a `ALLOWED_HOSTS` y `https://…` a `CSRF_TRUSTED_ORIGINS`;
- usa como caché el Redis de `CELERY_BROKER_URL` en la BD 3.

Para cambiarlo: `ANIVERSARIO_HOST=otro.dominio` / `ANIVERSARIO_CACHE_URL=redis://…` en el `.env`.
`ANIVERSARIO_HOST=` (vacío) apaga el subdominio.

## 6. Reinicio

```bash
sudo systemctl restart iesppab && systemctl status iesppab --no-pager | head -5
```

## 7. Verificación

1. `curl -sI https://aniversario.iesp-alfonsobarranteslingan.edu.pe/` → 200.
2. `curl -sI https://aniversario.iesp-alfonsobarranteslingan.edu.pe/admin/` → 404.
3. `curl -sI https://aniversario.iesp-alfonsobarranteslingan.edu.pe/media/private/` → 404.
4. Originalidad: login, subir un documento, ver un reporte.
5. Admin de originalidad → Concurso (fechas reales) y Candidatos (hasta 5 fotos y el video). Votar desde un celular,
   anotar el comprobante y borrar ese voto en Admin → Votos (solo superusuario).

## Retiro al terminar el concurso (no ejecutar hasta que dirección lo pida)

1. Admin → Concursos → seleccionar → "Exportar resultados a CSV". Guardar también un `pg_dump`.
2. `sudo rm /etc/nginx/sites-enabled/aniversario && sudo nginx -t && sudo systemctl reload nginx`.
3. Poner `ANIVERSARIO_HOST=` (vacío) en el `.env` y reiniciar `iesppab`: el middleware no hace nada y el host
   sale de `ALLOWED_HOSTS`/`CSRF_TRUSTED_ORIGINS`.
4. Ley 29733: borrar IP/token de los votos una vez publicados los resultados
   (`Voto.objects.update(ip=None, user_agent_hash="")`, desde `manage.py shell`).
5. Opcional, sacar la app: `python manage.py migrate aniversario zero`, quitar `apps.aniversario` de
   `INSTALLED_APPS`, su middleware, el alias `aniversario` de `CACHES`, restaurar
   `CSRF_FAILURE_VIEW = "apps.core.views.csrf_failure"`, y borrar `apps/aniversario/` y `media/aniversario/`.
   Originalidad no depende de nada de esto.
