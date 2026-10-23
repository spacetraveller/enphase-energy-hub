import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))

from compare_daily_solar_estimates import main  # noqa: E402


if __name__ == "__main__":
    sys.argv.extend(["--mode", "latest-sources"])
    main()
