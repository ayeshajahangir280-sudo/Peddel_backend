from django.urls import path

from . import views

urlpatterns = [
    path("tournaments/", views.TournamentListView.as_view(), name="public-tournaments"),
    path("tournaments/<slug:slug>/", views.TournamentDetailView.as_view(), name="public-tournament"),
    path("matches/<str:public_id>/", views.MatchDetailView.as_view(), name="public-match"),
]
