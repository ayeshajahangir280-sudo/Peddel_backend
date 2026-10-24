from django.contrib import admin
from django.urls import include, path, re_path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter

from apps.accounts.views import UserViewSet
from apps.audit.views import AuditLogViewSet
from apps.matches.views import CourtViewSet, DashboardView, MatchViewSet
from apps.players.views import PlayerViewSet
from apps.public_api import views as public_views
from apps.public_api.media import serve_media
from apps.teams.views import TeamViewSet
from apps.tournaments.views import TournamentViewSet

router = DefaultRouter()
router.register("players", PlayerViewSet, basename="admin-player")
router.register("teams", TeamViewSet, basename="admin-team")
router.register("courts", CourtViewSet, basename="admin-court")
router.register("matches", MatchViewSet, basename="admin-match")
router.register("tournaments", TournamentViewSet, basename="admin-tournament")
router.register("users", UserViewSet, basename="admin-user")
router.register("audit", AuditLogViewSet, basename="admin-audit")

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("api/health/", public_views.health),
    path("api/auth/", include("apps.accounts.urls")),
    path("api/admin/dashboard/", DashboardView.as_view()),
    path("api/admin/reports/", include("apps.reports.urls")),  # authenticated, admin-only PDFs
    path("api/admin/", include(router.urls)),
    path("api/public/", include("apps.public_api.urls")),  # read-only, no PDFs
    path("share/t/<slug:slug>/", public_views.share_tournament),
    path("share/m/<str:public_id>/", public_views.share_match),
    re_path(r"^media/(?P<path>.+)$", serve_media),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema")),
]
