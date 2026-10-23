import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))

from query_energy_flow import main  # noqa: E402


if __name__ == "__main__":
    main(["--start=-10m", "--fields", "w_now"])
