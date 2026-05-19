"""Constants for the Wilma integration."""

DOMAIN = "wilma_school_ai"

CONF_BASE_URL = "base_url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_CHILDREN = "children"
CONF_SENDER_FILTERS = "sender_filters"
CONF_MESSAGE_LIMIT = "message_limit"

DEFAULT_SCAN_INTERVAL = 14400  # 4 hours
DEFAULT_MESSAGE_LIMIT = 10

EVENT_NEW_EXAM = "wilma_school_ai_new_exam"
EVENT_NEW_MESSAGE = "wilma_school_ai_new_message"

# How far ahead the schedule calendar exposes events. The /overview endpoint
# returns the full school-year span anyway; this is just a safety cap on the
# range a Lovelace card can request.
SCHEDULE_LOOKAHEAD_DAYS = 28

# How far back homework entries are surfaced on the homework sensor.
# Wilma's diary stretches back to the start of the school year; surfacing
# it all would push hundreds of stale items into HA attributes.
HOMEWORK_LOOKBACK_DAYS = 14
