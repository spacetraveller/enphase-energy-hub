import os
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.append(str(Path(__file__).resolve().parents[2]))

from record.query_helpers import flux_time, query_measurement_rows  # noqa: E402


def main() -> None:
    measurement = os.getenv("CADENCE_MEASUREMENT", "enphase_energy_flow")
    field = os.getenv("CADENCE_FIELD", "w_now")
    timezone_name = os.getenv("CADENCE_TIMEZONE", "UTC")
    local_tz = ZoneInfo(timezone_name)
    today = datetime.now(local_tz).date()
    target_date = today - timedelta(days=int(os.getenv("CADENCE_DAYS_AGO", "1")))
    start = datetime.combine(target_date, datetime.min.time(), tzinfo=local_tz)
    stop = start + timedelta(days=1)

    times = [
        row["_time"]
        for row in query_measurement_rows(
            measurement,
            start=flux_time(start),
            stop=flux_time(stop),
            fields=[field],
            filters=['r.section == "production"', 'r.measurement_type == "production"'],
            keep_columns=["_time"],
        )
    ]

    unique_times = sorted(set(times))
    gaps = [
        round((unique_times[index] - unique_times[index - 1]).total_seconds())
        for index in range(1, len(unique_times))
    ]
    gap_counts = Counter(gaps)
    total_gaps = len(gaps)
    near_30 = sum(count for gap, count in gap_counts.items() if 25 <= gap <= 35)
    near_60 = sum(count for gap, count in gap_counts.items() if 55 <= gap <= 65)

    print(f"measurement={measurement} field={field}")
    print(f"local_day={target_date.isoformat()} timezone={timezone_name}")
    print(f"points={len(unique_times)} gaps={total_gaps}")
    if unique_times:
        print(f"first={unique_times[0].isoformat()} last={unique_times[-1].isoformat()}")
    print(f"near_30s={near_30} near_60s={near_60}")
    if total_gaps:
        print(f"near_30s_percent={near_30 / total_gaps * 100:.1f}")
        print(f"near_60s_percent={near_60 / total_gaps * 100:.1f}")
    print("top_gaps_seconds=" + str(gap_counts.most_common(20)))


if __name__ == "__main__":
    main()
