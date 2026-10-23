import argparse

from cloud_common import env_required, token_request, write_token_env


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh Enphase OAuth tokens.")
    parser.add_argument("--write-env", default="", help="Update token fields in this gitignored env file.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": env_required("ENPHASE_CLOUD_REFRESH_TOKEN"),
        }
    )
    print(
        "token_refresh_ok=true "
        f"token_type={payload.get('token_type')} "
        f"expires_in={payload.get('expires_in')} "
        f"has_refresh_token={bool(payload.get('refresh_token'))}"
    )
    if args.write_env:
        output_path = write_token_env(args.write_env, payload)
        print(f"wrote_token_env={output_path}")


if __name__ == "__main__":
    main()
