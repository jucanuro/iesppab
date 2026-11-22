#!/usr/bin/env bash
# Configura Nginx + HTTPS del subdominio de aniversario en el servidor de originalidad.
# Idempotente: se puede volver a correr. NO toca el bloque de originalidad.
#
#   sudo bash deploy/aniversario/instalar_nginx.sh            # Nginx + certbot
#   sudo bash deploy/aniversario/instalar_nginx.sh --sin-certbot
#
# Variables opcionales: ANIV_HOST, ANIV_APP_DIR, ANIV_UPSTREAM (si no se detecta solo).
set -euo pipefail

HOST="${ANIV_HOST:-aniversario.iesp-alfonsobarranteslingan.edu.pe}"
APP_DIR="${ANIV_APP_DIR:-/var/www/projects/iesppab}"
SITE=/etc/nginx/sites-available/aniversario
PROXY_INC=/etc/nginx/aniversario_proxy.conf
CERT=/etc/letsencrypt/live/$HOST/fullchain.pem
CON_CERTBOT=1
[[ "${1:-}" == "--sin-certbot" ]] && CON_CERTBOT=0

[[ $EUID -eq 0 ]] || { echo "Ejecutar con sudo."; exit 1; }
[[ -d "$APP_DIR/staticfiles" ]] || { echo "Falta $APP_DIR/staticfiles: corre collectstatic primero."; exit 1; }

# 1) Upstream: el mismo proxy_pass que ya usa originalidad.
UPSTREAM="${ANIV_UPSTREAM:-}"
if [[ -z "$UPSTREAM" ]]; then
  UPSTREAM=$(grep -RhoE 'proxy_pass\s+[^;]+' /etc/nginx/sites-enabled/ 2>/dev/null \
    | grep -v aniversario | awk '{print $2}' | sort | uniq -c | sort -rn | awk 'NR==1{print $2}' || true)
fi
[[ -n "$UPSTREAM" ]] || { echo "No encontré proxy_pass de originalidad. Usa ANIV_UPSTREAM=http://127.0.0.1:8000"; exit 1; }
echo "Upstream de Gunicorn: $UPSTREAM"

# 2) Respaldo de lo que vamos a sobrescribir.
STAMP=$(date +%Y%m%d_%H%M%S)
mkdir -p /root/nginx_backups
for f in "$SITE" "$PROXY_INC"; do [[ -f "$f" ]] && cp "$f" "/root/nginx_backups/$(basename "$f").$STAMP"; done

# 3) Carpeta pública de fotos/videos (solo esta sale por el subdominio).
MEDIA="$APP_DIR/media"
mkdir -p "$MEDIA/aniversario"
chown --reference="$MEDIA" "$MEDIA/aniversario"

cat > "$PROXY_INC" <<CONF
proxy_pass $UPSTREAM;
proxy_set_header Host \$host;
proxy_set_header X-Real-IP \$remote_addr;
proxy_set_header X-Forwarded-For \$remote_addr;
proxy_set_header X-Forwarded-Proto \$scheme;
proxy_read_timeout 60s;
CONF

cuerpo() {
cat <<CONF
    client_max_body_size 15m;

    location /static/ {
        alias $APP_DIR/staticfiles/;
        expires 7d;
        access_log off;
    }
    # SOLO lo del concurso: media/ también contiene las tesis privadas.
    location /media/aniversario/ {
        alias $MEDIA/aniversario/;
        expires 1d;
    }
    location /media/ { return 404; }

    location = /votar/confirmar/ {
        limit_req zone=aniv_voto burst=30 nodelay;
        limit_req_status 429;
        include $PROXY_INC;
    }
    location / {
        include $PROXY_INC;
    }
CONF
}

escribir_sitio() {
  SSL_EXTRA="    ssl_protocols TLSv1.2 TLSv1.3;"
  [[ -f /etc/letsencrypt/options-ssl-nginx.conf ]] && SSL_EXTRA="    include /etc/letsencrypt/options-ssl-nginx.conf;"
  [[ -f /etc/letsencrypt/ssl-dhparams.pem ]] && SSL_EXTRA="$SSL_EXTRA
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;"
  {
    echo "# Generado por deploy/aniversario/instalar_nginx.sh — no editar a mano."
    # Nombre largo: el tamaño por defecto (64) no alcanza. Solo si nginx.conf no lo define ya.
    if ! grep -qsE '^\s*server_names_hash_bucket_size' /etc/nginx/nginx.conf /etc/nginx/conf.d/*.conf; then
      echo "server_names_hash_bucket_size 128;"
    fi
    echo "limit_req_zone \$binary_remote_addr zone=aniv_voto:10m rate=30r/m;"
    echo
    if [[ -f "$CERT" ]]; then
      cat <<CONF
server {
    listen 80;
    server_name $HOST;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://\$host\$request_uri; }
}

server {
    listen 443 ssl http2;
    server_name $HOST;
    ssl_certificate $CERT;
    ssl_certificate_key /etc/letsencrypt/live/$HOST/privkey.pem;
$SSL_EXTRA

CONF
    else
      cat <<CONF
server {
    listen 80;
    server_name $HOST;
    location /.well-known/acme-challenge/ { root /var/www/html; }

CONF
    fi
    cuerpo
    echo "}"
  } > "$SITE"
  ln -sf "$SITE" /etc/nginx/sites-enabled/aniversario
}

recargar() {
  if ! nginx -t; then
    echo "nginx -t falló: restauro y no recargo."
    for f in "$SITE" "$PROXY_INC"; do
      b="/root/nginx_backups/$(basename "$f").$STAMP"
      if [[ -f "$b" ]]; then cp "$b" "$f"; else rm -f "$f"; fi
    done
    [[ -f "$SITE" ]] || rm -f /etc/nginx/sites-enabled/aniversario
    exit 1
  fi
  systemctl reload nginx
}

escribir_sitio
recargar
echo "Nginx listo para $HOST."

# 4) Certificado (webroot: certbot no edita nuestro archivo; luego lo regeneramos con 443).
if [[ ! -f "$CERT" && $CON_CERTBOT -eq 1 ]]; then
  mkdir -p /var/www/html
  certbot certonly --webroot -w /var/www/html -d "$HOST" --non-interactive --keep-until-expiring
  # Renovación: recargar Nginx tras renovar.
  sed -i '/^renew_hook/d' "/etc/letsencrypt/renewal/$HOST.conf" 2>/dev/null || true
  grep -q '^\[renewalparams\]' "/etc/letsencrypt/renewal/$HOST.conf" && \
    sed -i '/^\[renewalparams\]/a renew_hook = systemctl reload nginx' "/etc/letsencrypt/renewal/$HOST.conf"
  escribir_sitio
  recargar
  echo "HTTPS listo: https://$HOST/"
fi

echo
echo "Verificación:"
for ruta in / /admin/ /media/private/; do
  printf '  %-16s %s\n' "$ruta" "$(curl -s -o /dev/null -w '%{http_code}' -k "https://$HOST$ruta" || echo '-')"
done
echo "Esperado: / 200, /admin/ 404, /media/private/ 404."
