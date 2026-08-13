"""Konstanten für die pooldose_live-Integration."""

DOMAIN = "pooldose_live"
MANUFACTURER = "SEKO"

ISSUE_FW_FALLBACK = "fw_fallback"
ISSUE_RAW_MODE = "raw_mode"
ISSUES_URL = "https://github.com/erazorlll/seko-pooldose-live/issues"

# Konzept §5.3: 90s liegt über normalem Tick-Jitter, aber weit unter den in
# §8.2/§8.3 gemessenen echten Aussetzern (bis 9,7 Minuten) - siehe
# pooldose_live.transport.DEFAULT_STALENESS_TIMEOUT für die volle Begründung.
