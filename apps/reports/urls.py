"""Protected administrative report routes. Mounted ONLY under /api/admin/reports/ (never under /api/public/)."""
from django.urls import path

from .views import MatchPdfView, TournamentPdfView

urlpatterns = [
    path("matches/<int:pk>/pdf/", MatchPdfView.as_view(), name="report-match-pdf"),
    path("tournaments/<int:pk>/pdf/", TournamentPdfView.as_view(), {"kind": "full"}, name="report-tournament-pdf"),
    path("tournaments/<int:pk>/standings/pdf/", TournamentPdfView.as_view(), {"kind": "standings"}, name="report-standings-pdf"),
    path("tournaments/<int:pk>/fixtures/pdf/", TournamentPdfView.as_view(), {"kind": "fixtures"}, name="report-fixtures-pdf"),
]
