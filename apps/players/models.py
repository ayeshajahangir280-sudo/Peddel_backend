import uuid

from django.conf import settings
from django.db import models


def player_photo_path(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    return f"players/{uuid.uuid4().hex}.{ext}"


class Player(models.Model):
    first_name = models.CharField(max_length=60)
    last_name = models.CharField(max_length=60)
    # Private contact details: never exposed by public APIs.
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=32, blank=True)
    photo = models.ImageField(upload_to=player_photo_path, blank=True)
    show_photo_publicly = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["first_name", "last_name", "id"]
        indexes = [models.Index(fields=["last_name", "first_name"])]

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def __str__(self):
        return self.full_name
