#!/usr/bin/python

import argparse
import requests
import time
import sys


# Request githubv3 json responses
ACCEPT_HEADERS = {
    "Accept": "application/vnd.github.v3+json",
}

SHIFTSTACK_MERGE_BOT_CLONER = "951bc5999667be640e63"


class RequestError(Exception):
    pass


def wait_for_authorisation(client_id, device_code, poll_interval):
    token_request = {
        "client_id": client_id,
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }

    while True:
        time.sleep(poll_interval)

        resp = requests.post(
            "https://github.com/login/oauth/access_token",
            data=token_request,
            headers=ACCEPT_HEADERS,
        )
        resp.raise_for_status()
        token = resp.json()

        error = token.get("error")
        if error is None:
            return token["access_token"]

        # User hasn't authorised the request, yet
        if error == "authorization_pending":
            continue

        # We're sending requests too fast. Wait an additional period before
        # polling again.
        # This should never happen because we're already waiting the requested
        # interval between requests.
        if error == "slow_down":
            time.sleep(token["interval"] - poll_interval)
            continue

        raise RequestError(f'{error}: {token["error_description"]}')


def authorise(client_id):
    code_request = {
        "client_id": client_id,
        "scope": "repo",
    }

    resp = requests.post(
        "https://github.com/login/device/code",
        data=code_request,
        headers=ACCEPT_HEADERS,
    )
    resp.raise_for_status()
    code = resp.json()

    print(
        f'Go to {code["verification_uri"]}, enter code {code["user_code"]}, '
        f"and authorise the app"
    )

    return wait_for_authorisation(client_id, code["device_code"], code["interval"])


def main():
    parser = argparse.ArgumentParser(description="Request an OAUTH token")
    parser.add_argument(
        "--client-id", type=str, required=False, default=SHIFTSTACK_MERGE_BOT_CLONER
    )
    args = parser.parse_args()

    try:
        access_token = authorise(args.client_id)
        print(f"Access token: {access_token}")
    except RequestError as ex:
        print(str(ex), file=sys.stderr)
        exit(1)


if __name__ == "__main__":
    main()
