"""Cloudinary-backed default storage (used when CLOUDINARY_* env vars are set).

File names stay exactly as before ("players/<uuid>.jpg"), so the DB and the /media/<name> URLs don't change;
the image itself lives on Cloudinary at <CLOUDINARY_FOLDER>/players/<uuid>.
"""
import urllib.request

import cloudinary.uploader
import cloudinary.utils
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible


@deconstructible
class CloudinaryStorage(Storage):
    def _public_id(self, name):
        stem = name.rsplit(".", 1)[0]
        folder = settings.CLOUDINARY_FOLDER.strip("/")
        return f"{folder}/{stem}" if folder else stem

    def _save(self, name, content):
        content.seek(0)
        cloudinary.uploader.upload(content, public_id=self._public_id(name), resource_type="image", overwrite=False)
        return name

    def _open(self, name, mode="rb"):
        with urllib.request.urlopen(self.url(name), timeout=10) as resp:
            return ContentFile(resp.read(), name=name)

    def url(self, name):
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else None
        return cloudinary.utils.cloudinary_url(self._public_id(name), secure=True, format=ext, resource_type="image")[0]

    def exists(self, name):
        return False  # upload names are random UUIDs; skips an API round-trip per upload

    def delete(self, name):
        if name:
            cloudinary.uploader.destroy(self._public_id(name), resource_type="image", invalidate=True)
