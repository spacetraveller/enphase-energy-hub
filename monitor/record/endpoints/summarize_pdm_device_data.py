import os
from collections import Counter
from typing import Any

import requests
import urllib3


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def main() -> None:
    host = env_required("ENPHASE_HOST").strip("/")
    scheme = os.getenv("ENPHASE_API_SCHEME", "https").strip(":/")
    request_host = os.getenv("ENPHASE_REQUEST_HOST", "").strip("/")
    timeout = int(os.getenv("PROBE_TIMEOUT_SECONDS", "10"))
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {env_required('ENPHASE_ACCESS_TOKEN')}",
        "Connection": "close",
    }
    if request_host:
        headers["Host"] = request_host

    response = requests.get(
        f"{scheme}://{host}/ivp/pdm/device_data",
        headers=headers,
        timeout=timeout,
        verify=False,
        allow_redirects=False,
    )
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    counts: Counter[str] = Counter()
    print(f"deviceCount={payload.get('deviceCount')} deviceDataLimit={payload.get('deviceDataLimit')}")
    for eid, item in sorted(payload.items()):
        if not isinstance(item, dict):
            continue
        dev_name = str(item.get("devName", ""))
        counts[dev_name] += 1
        serial = item.get("sn", "")
        channel_count = len(item.get("channels", [])) if isinstance(item.get("channels"), list) else 0
        print(f"{eid} devName={dev_name} sn={serial} active={item.get('active')} channels={channel_count}")

    print(f"devName_counts={dict(counts)}")


if __name__ == "__main__":
    main()
