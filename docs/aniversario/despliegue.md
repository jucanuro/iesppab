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

## 4. Nginx

**4a. Bloque de originalidad (único cambio ahí):** los videos se suben desde el admin, que está en el dominio
de originalidad. Subir `client_max_body_size` de ese bloque a `70m` (videos de hasta 60 MB). Ver primero el valor
actual con `sudo grep -rn client_max_body_size /etc/nginx/`. Antes de subir 14 videos, revisar `df -h /`
(14 × 60 MB ≈ 0,8 GB).

**4b. Bloque nuevo del subdominio:**

`/etc/nginx/sites-available/aniversario` — primero solo HTTP (certbot agrega el 443):

```nginx
# En el contexto http (p. ej. /etc/nginx/conf.d/aniversario_limits.conf):
limit_req_zone $binary_remote_addr zone=aniv_voto:10m rate=6r/m;

server {
    listen 80;
    server_name aniversario.iesp-alfonsobarranteslingan.edu.pe;

    client_max_body_size 15m;   # fotos que se suben desde el admin (que está en el otro host)

    location /static/ {
        alias /var/www/projects/iesppab/staticfiles/;
        expires 7d;
    }
    # SOLO las fotos del concurso: media/ también contiene las tesis privadas.
    location /media/aniversario/ {
        alias /var/www/projects/iesppab/media/aniversario/;
        expires 1d;
        # Los videos se sirven con soporte de rangos (Nginx lo hace solo): el navegador puede adelantar.
    }
    location /media/ { return 404; }

    location = /votar/confirmar/ {
        limit_req zone=aniv_voto burst=5 nodelay;
        limit_req_status 429;
        include /etc/nginx/aniversario_proxy.conf;
    }
    location / {
        include /etc/nginx/aniversario_proxy.conf;
    }
}
```

`/etc/nginx/aniversario_proxy.conf`:

```nginx
proxy_pass http://<upstream o unix:/ruta/gunicorn.sock>;   # el mismo que usa originalidad
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;                    # sobrescribe lo que mande el cliente
proxy_set_header X-Forwarded-For $remote_addr;              # no se encadena el del cliente
proxy_set_header X-Forwarded-Proto $scheme;
```

```bash
sudo ln -s /etc/nginx/sites-available/aniversario /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## 5. HTTPS

```bash
sudo certbot --nginx -d aniversario.iesp-alfonsobarranteslingan.edu.pe
sudo nginx -t && sudo systemctl reload nginx
```

## 6. Variables de entorno

Agregar en `/var/www/projects/iesppab/.env` (respaldar antes: `cp .env .env.bak_$(date +%Y%m%d)`):

```
ANIVERSARIO_HOST=aniversario.iesp-alfonsobarranteslingan.edu.pe
ANIVERSARIO_CACHE_URL=redis://127.0.0.1:<puerto>/3      # BD distinta a la de Celery
```

Y añadir el host a las listas existentes (sin quitar nada):
`ALLOWED_HOSTS=...,aniversario.iesp-alfonsobarranteslingan.edu.pe` y
`CSRF_TRUSTED_ORIGINS=...,https://aniversario.iesp-alfonsobarranteslingan.edu.pe`.

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
3. Quitar `ANIVERSARIO_HOST` (y el host de `ALLOWED_HOSTS`/`CSRF_TRUSTED_ORIGINS`) del `.env`; reiniciar `iesppab`.
   Con `ANIVERSARIO_HOST` vacío el middleware no hace nada.
4. Ley 29733: borrar IP/token de los votos una vez publicados los resultados
   (`Voto.objects.update(ip=None, user_agent_hash="")`, desde `manage.py shell`).
5. Opcional, sacar la app: `python manage.py migrate aniversario zero`, quitar `apps.aniversario` de
   `INSTALLED_APPS`, su middleware, el alias `aniversario` de `CACHES`, restaurar
   `CSRF_FAILURE_VIEW = "apps.core.views.csrf_failure"`, y borrar `apps/aniversario/` y `media/aniversario/`.
   Originalidad no depende de nada de esto.
