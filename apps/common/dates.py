from django.utils.dateparse import parse_date


def parse_query_date(raw):
    """A ``YYYY-MM-DD`` query value as a date, or None if missing or invalid."""
    try:
        return parse_date(raw or "")
    except ValueError:
        # Right shape, impossible date (for example month 13).
        return None
