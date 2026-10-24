from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models


class Role(models.TextChoices):
    SUPER_ADMIN = "super_admin", "Super Admin"
    TOURNAMENT_ADMIN = "tournament_admin", "Tournament Admin"
    UMPIRE = "umpire", "Umpire / Match Operator"


class PadelUserManager(UserManager):
    def create_superuser(self, username, email=None, password=None, **extra):
        extra.setdefault("role", Role.SUPER_ADMIN)
        return super().create_superuser(username, email, password, **extra)


class User(AbstractUser):
    email = models.EmailField(unique=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.UMPIRE, db_index=True)
    phone = models.CharField(max_length=32, blank=True)

    objects = PadelUserManager()
    REQUIRED_FIELDS = ["email"]

    @property
    def is_super_admin(self):
        return self.role == Role.SUPER_ADMIN

    @property
    def is_tournament_admin(self):
        return self.role == Role.TOURNAMENT_ADMIN

    @property
    def is_umpire(self):
        return self.role == Role.UMPIRE

    @property
    def is_admin_role(self):
        return self.role in (Role.SUPER_ADMIN, Role.TOURNAMENT_ADMIN)

    @property
    def display_name(self):
        return self.get_full_name() or self.username

    def __str__(self):
        return f"{self.username} ({self.role})"
