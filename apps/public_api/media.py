"""Controlled media serving: uploaded images are never exposed unless they belong to public content."""
from django.conf import settings
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404, HttpResponseRedirect
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import TokenError

from apps.players.models import Player
from apps.tournaments.models import Tournament


def _token_user(request):
    token = request.GET.get("t")
    if not token:
        return None
    try:
        auth = JWTAuthentication()
        return auth.get_user(auth.get_validated_token(token))
    except (TokenError, Exception):
        return None


def serve_media(request, path):
    name = path.lstrip("/")
    if ".." in name or name.startswith(("/", "\\")):
        raise Http404
    if not settings.USE_CLOUDINARY:
        full = (settings.MEDIA_ROOT / name).resolve()
        if settings.MEDIA_ROOT.resolve() not in full.parents or not full.is_file():
            raise Http404
    allowed = False
    if name.startswith("tournaments/"):
        for t in Tournament.objects.filter(logo=name) | Tournament.objects.filter(cover_image=name):
            if t.is_public:
                allowed = True
    elif name.startswith("players/"):
        allowed = Player.objects.filter(photo=name, show_photo_publicly=True).exists()
    if not allowed:
        user = _token_user(request)
        allowed = bool(user and user.is_active and user.is_admin_role)
    if not allowed:
        raise Http404
    if settings.USE_CLOUDINARY:
        resp = HttpResponseRedirect(default_storage.url(name))
    else:
        resp = FileResponse(open(full, "rb"))
    resp["Cache-Control"] = "public, max-age=3600" if not request.GET.get("t") else "private, no-store"
    return resp
