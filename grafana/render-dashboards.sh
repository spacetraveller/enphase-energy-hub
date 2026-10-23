#!/bin/sh
set -eu
: "${HISTORY_TIMEZONE:=UTC}"
: "${METER_PRODUCTION_EID:=}"
: "${METER_NET_EID:=}"

# Only decimal amounts are allowed in generated dashboard JSON and Flux queries.
validate_rate() {
    if ! printf '%s\n' "$2" | grep -Eq '^[0-9]+([.][0-9]+)?$'; then
        echo "$1 must be a non-negative decimal amount in AUD/kWh" >&2
        exit 1
    fi
}

validate_rate IMPORT_RATE_AUD_PER_KWH "$IMPORT_RATE_AUD_PER_KWH"
validate_rate FEED_IN_RATE_AUD_PER_KWH "$FEED_IN_RATE_AUD_PER_KWH"
# Empty IDs keep generic installs valid; configured IDs must be decimal digits.
validate_eid() {
    if [ -n "$2" ] && ! printf '%s\n' "$2" | grep -Eq '^[0-9]+$'; then
        echo "$1 must contain decimal digits only" >&2
        exit 1
    fi
}

validate_eid METER_PRODUCTION_EID "$METER_PRODUCTION_EID"
validate_eid METER_NET_EID "$METER_NET_EID"

# Limit the timezone to characters valid in IANA timezone names and safe for sed.
if ! printf '%s\n' "$HISTORY_TIMEZONE" | grep -Eq '^[A-Za-z0-9_+./-]+$'; then
    echo "HISTORY_TIMEZONE must be a valid timezone name" >&2
    exit 1
fi

mkdir -p /tmp/enphase-dashboards
for source in /var/lib/grafana/dashboards/*.json; do
    target="/tmp/enphase-dashboards/$(basename "$source")"
    sed -e "s/__IMPORT_RATE_AUD_PER_KWH__/$IMPORT_RATE_AUD_PER_KWH/g" \
        -e "s/__FEED_IN_RATE_AUD_PER_KWH__/$FEED_IN_RATE_AUD_PER_KWH/g" \
        -e "s/__METER_PRODUCTION_EID__/$METER_PRODUCTION_EID/g" \
        -e "s/__METER_NET_EID__/$METER_NET_EID/g" \
        -e "s|__HISTORY_TIMEZONE__|$HISTORY_TIMEZONE|g" \
        "$source" > "$target"
done

exec /run.sh "$@"