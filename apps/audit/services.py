from .models import AuditLog


def _ip(request):
    if request is None:
        return None
    fwd = request.META.get("HTTP_X_FORWARDED_FOR")
    return (fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR")) or None


def log(actor, action, object_type, object_id="", details=None, request=None):
    """Record an audit entry. Never raises into the caller's business flow."""
    try:
        return AuditLog.objects.create(
            actor=actor if getattr(actor, "is_authenticated", False) else None,
            action=action,
            object_type=object_type,
            object_id=str(object_id),
            details=details or {},
            ip_address=_ip(request),
        )
    except Exception:  # pragma: no cover - audit must not break requests
        return None
