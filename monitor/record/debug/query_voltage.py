import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))

from query_meter_fields import main  # noqa: E402


if __name__ == "__main__":
    main(["--fields", "voltage", "--positive-only"])
