from django.conf import settings
from rest_framework import serializers

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


def validate_image(file):
    """Size + MIME checks. ImageField already verified the bytes decode as an image with Pillow."""
    if file.size > settings.MAX_UPLOAD_BYTES:
        raise serializers.ValidationError(f"Image too large (max {settings.MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
    ctype = getattr(file, "content_type", None)
    if ctype and ctype not in ALLOWED_IMAGE_TYPES:
        raise serializers.ValidationError("Only JPEG, PNG or WebP images are allowed.")
    return file
