import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import OriginValidator  # noqa: E402
from django.conf import settings  # noqa: E402

from apps.scoring.routing import websocket_urlpatterns  # noqa: E402

ws_app = URLRouter(websocket_urlpatterns)
if not settings.DEBUG:
    ws_app = OriginValidator(ws_app, settings.WS_ALLOWED_ORIGINS)

application = ProtocolTypeRouter({"http": django_asgi_app, "websocket": ws_app})
