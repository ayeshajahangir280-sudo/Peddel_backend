from django.urls import re_path

from .consumers import MatchConsumer

websocket_urlpatterns = [
    re_path(r"^ws/matches/(?P<public_id>[A-Z0-9]{6,16})/$", MatchConsumer.as_asgi()),
]
