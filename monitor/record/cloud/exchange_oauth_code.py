import argparse

from cloud_common import env_required, redirect_uri, token_request, write_token_env


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Exchange an Enphase OAuth code for tokens.")
    parser.add_argument("--write-env", default="", help="Update token fields in this gitignored env file.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = token_request(
        {
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri(),
            "code": env_required("ENPHASE_CLOUD_AUTH_CODE"),
        }
    )
    print(
        "token_exchange_ok=true "
        f"token_type={payload.get('token_type')} "
        f"expires_in={payload.get('expires_in')} "
        f"has_refresh_token={bool(payload.get('refresh_token'))}"
    )
    if args.write_env:
        output_path = write_token_env(args.write_env, payload)
        print(f"wrote_token_env={output_path}")


if __name__ == "__main__":
    main()
