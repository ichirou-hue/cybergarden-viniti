"""Small helpers for safe CSV export."""


def safe_csv_value(value):
    """Prevent spreadsheet formula evaluation for exported text values."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value
