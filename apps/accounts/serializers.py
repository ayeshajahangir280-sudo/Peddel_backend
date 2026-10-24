from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from .models import Role, User


class UserSerializer(serializers.ModelSerializer):
    display_name = serializers.CharField(read_only=True)
    password = serializers.CharField(write_only=True, required=False, style={"input_type": "password"})

    class Meta:
        model = User
        fields = [
            "id", "username", "email", "first_name", "last_name", "display_name",
            "role", "phone", "is_active", "password", "date_joined",
        ]
        read_only_fields = ["id", "date_joined"]

    def validate_password(self, value):
        validate_password(value)
        return value

    def validate_role(self, value):
        request = self.context.get("request")
        if request and not request.user.is_super_admin and value != Role.UMPIRE:
            raise serializers.ValidationError("Only a Super Admin can assign admin roles.")
        return value

    def create(self, validated):
        password = validated.pop("password", None)
        if not password:
            raise serializers.ValidationError({"password": "Required."})
        user = User(**validated)
        user.set_password(password)
        user.save()
        return user

    def update(self, instance, validated):
        password = validated.pop("password", None)
        for k, v in validated.items():
            setattr(instance, k, v)
        if password:
            instance.set_password(password)
        instance.save()
        return instance


class UmpireBriefSerializer(serializers.ModelSerializer):
    display_name = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = ["id", "display_name"]
