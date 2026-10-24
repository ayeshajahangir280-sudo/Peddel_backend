from rest_framework import serializers

from apps.accounts.validators import validate_image

from .models import Player


class PlayerSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    photo = serializers.ImageField(required=False, allow_null=True, validators=[validate_image], write_only=True)
    photo_url = serializers.SerializerMethodField()

    class Meta:
        model = Player
        fields = ["id", "first_name", "last_name", "full_name", "email", "phone", "photo", "photo_url",
                  "show_photo_publicly", "created_at"]
        read_only_fields = ["id", "created_at"]

    def get_photo_url(self, obj):
        return f"/media/{obj.photo.name}" if obj.photo else None

    def validate_first_name(self, v):
        v = v.strip()
        if not v:
            raise serializers.ValidationError("Required.")
        return v

    def validate(self, attrs):
        if attrs.get("photo") is None and "photo" in attrs:
            attrs.pop("photo")
        return attrs


class PlayerBriefSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = Player
        fields = ["id", "full_name"]
