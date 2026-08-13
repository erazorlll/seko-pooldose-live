"""Constants for the pooldose_live integration."""

DOMAIN = "pooldose_live"
MANUFACTURER = "SEKO"

ISSUE_FW_FALLBACK = "fw_fallback"
ISSUE_RAW_MODE = "raw_mode"
ISSUES_URL = "https://github.com/erazorlll/seko-pooldose-live/issues"

# Concept §5.3: 90s is above normal tick jitter, but far below the real
# dropouts measured in §8.2/§8.3 (up to 9.7 minutes) - see
# pooldose_live.transport.DEFAULT_STALENESS_TIMEOUT for the full rationale.
