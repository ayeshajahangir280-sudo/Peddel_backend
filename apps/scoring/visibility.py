from apps.matches.models import MatchStatus


def is_match_public(match):
    """Public when its tournament is published+public (tournament matches), or it is a shareable friendly.
    Draft matches are never public."""
    if match.status == MatchStatus.DRAFT:
        return False
    if match.tournament_id:
        return match.tournament.is_public
    return match.is_public
