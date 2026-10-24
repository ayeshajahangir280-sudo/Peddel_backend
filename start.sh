#!/bin/sh
set -e

echo "[start] applying migrations"
python manage.py migrate --noinput
python manage.py collectstatic --noinput
# Creates the first Super Admin only if ADMIN_USERNAME/ADMIN_PASSWORD are set and the user doesn't exist.
python manage.py ensure_admin

# Re-publishes WebSocket events that failed to send (transactional outbox).
if [ "${RUN_OUTBOX:-true}" = "true" ]; then
  python manage.py dispatch_outbox --loop --interval 3 &
fi

# Without Redis the channel layer is in-memory, so WebSocket fan-out only works inside ONE process.
WORKERS="${WEB_CONCURRENCY:-1}"
if [ -z "${REDIS_URL}" ]; then WORKERS=1; fi
echo "[start] starting uvicorn on port ${PORT:-8000} with ${WORKERS} worker(s)"
exec uvicorn config.asgi:application --host 0.0.0.0 --port "${PORT:-8000}" --workers "${WORKERS}" \
  --proxy-headers --forwarded-allow-ips="*" --ws-max-size 65536 --limit-concurrency "${MAX_CONNECTIONS:-1000}"
