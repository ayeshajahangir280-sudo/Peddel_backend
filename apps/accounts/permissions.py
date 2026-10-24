from rest_framework.permissions import BasePermission


class IsAdminRole(BasePermission):
    """Super Admin or Tournament Admin."""

    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and u.is_admin_role)


class IsSuperAdmin(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and u.is_super_admin)


class IsStaffRole(BasePermission):
    """Any authenticated operator role (admins and umpires)."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)


def can_manage_tournament(user, tournament):
    if not user.is_authenticated:
        return False
    if user.is_super_admin:
        return True
    if user.is_tournament_admin:
        return tournament.owner_id == user.id or tournament.managers.filter(pk=user.pk).exists()
    return False


def can_manage_match(user, match):
    """Administrative control of a match (edit, correct, report)."""
    if not user.is_authenticated:
        return False
    if user.is_super_admin:
        return True
    if user.is_tournament_admin:
        if match.tournament_id:
            return can_manage_tournament(user, match.tournament)
        return match.created_by_id == user.id
    return False


def can_score_match(user, match):
    """Operating a match live: admins who manage it, or the assigned umpire."""
    if can_manage_match(user, match):
        return True
    return bool(user.is_authenticated and user.is_umpire and match.umpire_id == user.id)
