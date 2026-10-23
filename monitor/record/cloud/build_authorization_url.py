import os

from cloud_common import auth_url, redirect_uri, with_query


def main() -> None:
    state = os.getenv("ENPHASE_CLOUD_AUTH_STATE", "")
    url = with_query(auth_url(), {"redirect_uri": redirect_uri(), "state": state})
    print(url)


if __name__ == "__main__":
    main()
