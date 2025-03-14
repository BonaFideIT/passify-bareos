#!/usr/bin/env python3

import re
import ssl
import sys
import json
import socket
import base64
import pathlib
import os.path
import hashlib
import argparse
import datetime
import configparser
from getpass import getpass
from urllib.parse import urlparse
from urllib.error import HTTPError
from http.client import HTTPSConnection
from urllib.request import Request, urlopen, build_opener, install_opener, HTTPSHandler

"""An icinga check_command wrapper to icinga api for submitting passive check results"""


class FingerprintedHTTPSConnection(HTTPSConnection):
    """HTTPSConnection with fingerprint verification instead of full-chain checks"""

    def __init__(self, host, expected_fingerprint, **kwargs):
        self.expected_fingerprint = expected_fingerprint
        super().__init__(host, **kwargs)

    def connect(self) -> None:
        """Implement fingerprint verification instead of default TLS chain verification"""

        # disable complete certificate chain verification, rely on fingerprint instead
        ctx = ssl._create_unverified_context()

        # create tls connection
        self.sock = ctx.wrap_socket(
            socket.create_connection((self.host, self.port)), server_hostname=self.host
        )

        # calculate fingerprint
        cert_der = self.sock.getpeercert(binary_form=True)
        actual_fingerprint = hashlib.sha256(cert_der).hexdigest()

        # compare fingerprints and raise exception
        if actual_fingerprint != self.expected_fingerprint:
            raise ssl.SSLError(
                f"Certificate fingerprint mismatch! "
                f"Expected: {self.expected_fingerprint}, "
                f"Got: {actual_fingerprint}"
                f"Please update your config with the new fingerprint, if applicable."
            )


class FingerprintHttpsHandler(HTTPSHandler):
    """Custom HTTPSHandler implementation to ensure peer certificate fingerprint matches"""

    def __init__(self, expected_fingerprint, **kwargs):
        """Accept and save expected fingerprint for later use"""

        super().__init__(**kwargs)
        self.expected_fingerprint = expected_fingerprint

    def https_open(self, request):
        """Install hook to fingerprint verifying connection-"""

        return self.do_open(self.fingerprint_verifying_connection, request)

    def fingerprint_verifying_connection(self, host, **kwargs):
        """Patch in custom HTTPSConnection class with fingerprint verification"""

        return FingerprintedHTTPSConnection(
            host, expected_fingerprint=self.expected_fingerprint, **kwargs
        )


def verify_url(url):
    """parse and verify url validity"""

    try:
        parsed = urlparse(url)
        if bool(parsed.scheme) and bool(parsed.netloc):
            return parsed
        return None
    except Exception:
        return None


def download_certificate(args, url):
    """download tls certificate from https url and display fingerprint for verification"""

    # download certificate
    ctx = ssl._create_unverified_context()
    port = url.port if url.port is not None else 443
    with socket.create_connection((url.hostname, port)) as sock:
        with ctx.wrap_socket(sock, server_hostname=url.hostname) as ssock:
            cert_der = ssock.getpeercert(binary_form=True)

    # calculate fingerprint
    fp = hashlib.sha256(cert_der).hexdigest()

    def format_fingerprint(fingerprint_hex: str) -> str:
        return ":".join(
            fingerprint_hex[i : i + 2] for i in range(0, len(fingerprint_hex), 2)
        ).upper()

    # ask for confirmation
    print("Please verfiy TLS fingerprint with certificate from master:")
    print(format_fingerprint(fp))
    print(
        "Hint: openssl x509 -in /var/lib/icinga2/certs/<hostname>.crt -noout -fingerprint -sha256"
    )
    yesno = input("Accept? [y/N]") == "y" or False
    if not yesno:
        raise Exception("Untrusted certificate, unable to continue.")

    return fp


def map_exit_status(value) -> int:
    """Map Bareos/Bacula exit_status value to icinga/nagios status code"""

    return {
        "OK": 0,
        "Error": 1,
        "Fatal Error": 2,
        "Canceled": 2,
        "Differences": 1,
        "Unknown term code": 2,
    }[value]


def extract_datetime(value) -> datetime.datetime:
    """Extract scheduled datetime from unique job id"""

    # Define the regex pattern to extract the datetime part
    DATETIME_REGEX = r"(\d{4}-\d{2}-\d{2}_\d{2}\.\d{2}\.\d{2})"

    match = re.search(DATETIME_REGEX, value)
    if not match:
        raise argparse.ArgumentTypeError(
            f"No valid datetime found in: {value}. Expected format: YYYY-MM-DD_HH.MM.SS"
        )

    return datetime.datetime.strptime(match.group(1), "%Y-%m-%d_%H.%M.%S")


def parse_args():
    """Parse command line arguments, capture known and unknown args"""

    parser = argparse.ArgumentParser(description=__doc__)
    configpath = pathlib.Path(__file__).parent.resolve() / "config.ini"
    parser.add_argument(
        "--config",
        type=pathlib.Path,
        default=configpath,
        help="Path where to store/load config from. [default=config.ini]",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Optional timeout for execution in seconds.",
    )
    parser.add_argument(
        "--ttl", type=int, default=None, help="TTL argument to pass to icinga api"
    )
    parser.add_argument(
        "-s",
        type=str,
        required=True,
        metavar="SERVICE NAME",
        help="Specify service name",
    )
    parser.add_argument(
        "--prefix-time",
        action="store_true",
        help="Enable prefixing the service name with the scheduled time.",
    )
    parser.add_argument(
        "type",
        choices=["Backup"],
        help="Bareos job type. Currently only Backup supported.",
    )
    parser.add_argument(
        "level",
        choices=["Full", "Differential", "Incremental", "VirtualFull"],
        help="Backup job level.",
    )
    parser.add_argument(
        "exit_status",
        choices=[
            "OK",
            "Error",
            "Fatal Error",
            "Canceled",
            "Differences",
            "Unknown term code",
        ],
        type=str,
        help="Backup job result value.",
    )
    parser.add_argument(
        "schedule",
        metavar="JOB_UID",
        type=extract_datetime,
        help="job unique name: e.g. jobname.date.time...",
    )
    parser.add_argument(
        "size",
        type=int,
        help="Number of pocessed bytes for the backup job.",
    )

    return parser.parse_args()


def load_config(args):
    """Load config from file, init with necessary values"""

    # prep config parser
    config = configparser.ConfigParser()
    default = config["DEFAULT"]
    if "TLS" not in config:
        config["TLS"] = {}
    authconfig = config["TLS"]

    # load config from file
    if os.path.isfile(args.config):
        config.read(args.config)
        if not {"url", "user", "password", "check_source"}.issubset(default) or not {
            "fingerprint"
        }.issubset(authconfig):
            raise Exception(
                "Invalid configuration file. Delete config file and try again."
            )

        return config

    # request api url until a valid url is provided
    durl = "https://localhost:5665"
    url = None
    while url is None:
        # ask for url
        default["url"] = input(f"Icinga API master url (default: {durl}):") or durl
        default["url"] += "/v1/actions/process-check-result"

        # parse and verify url
        url = verify_url(default["url"])
        if url is None:
            print("[ERROR] Invalid url, please specify a valid url.")

    # download certificate and validate fingerprint
    if url.scheme == "https":
        authconfig["fingerprint"] = download_certificate(args, url)

    # request check_source property
    hostname = socket.getfqdn()
    default["check_source"] = (
        input(f"Input host name (default:{hostname}):") or hostname
    )

    # request username
    duser = "passive"
    default["user"] = input(f"Icinga API username (default: {duser}):") or duser

    # request password
    default["password"] = getpass("Icinga API password:")

    # write config to file
    with open(args.config, "w+") as fd:
        config.write(fd)
        fd.flush()
        fd.close()

    return config


def human_readable_size(value, decimal_places=2) -> str:
    """Converts the value into a human readable string"""

    units = ["B", "KB", "MB", "GB", "TB", "PB", "EB"]
    factor = 1024

    for unit in units:
        if value < factor:
            return f"{value:.{decimal_places}f}{unit}"
        value /= factor

    return f"{value:.{decimal_places}f}{units[-1]}"


def build_request_data(args, default) -> dict:
    """build request data dictionary for icinga api passive result delivery"""

    data = {}

    # return status for icinga
    data["exit_status"] = map_exit_status(args.exit_status)

    # plugin output and performance data
    data["plugin_output"] = (
        f"{args.type} {args.level} {args.exit_status}: {args.schedule.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    data["performance_data"] = f"size={human_readable_size(args.size)};;"

    # pass check_command and check_source
    data["check_command"] = sys.argv
    data["check_source"] = default["check_source"]

    # pass ttl if provided
    if args.ttl:
        data["ttl"] = args.ttl

    # prefix service name if requested
    if args.prefix_time:
        sn = f"{args.schedule.strftime('%H:%M:%S')} {args.s}"
    else:
        sn = args.s

    # finally set required filter
    data["type"] = "Service"
    data["filter"] = (
        f"host.name==\"{default['check_source']}\" && service.name==\"{sn}\""
    )

    return data


def deliver(args, config) -> bool:
    """build api request and deliver passive result"""

    default = config["DEFAULT"]
    authconfig = config["TLS"]
    data = build_request_data(args, default)

    # create encoded credentials
    credentials = f"{default['user']}:{default['password']}"
    encoded_credentials = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")

    # headers
    headers = {
        "Accept": "application/json",
        "X-HTTP-Method-Override": "POST",
        "Authorization": f"Basic {encoded_credentials}",
    }

    # build opener for ssl fingerprint verification
    opener = build_opener(FingerprintHttpsHandler(authconfig["fingerprint"]))
    install_opener(opener)

    # build request
    request = Request(
        default["url"],
        data=bytes(json.dumps(data), encoding="utf-8"),
        headers=headers,
        method="post",
    )

    # execute request
    try:
        response = urlopen(request)
        if 200 <= response.status <= 299:
            return
        raise Exception(
            f"API endpoint returned non-success status code {response.status}: {response.read().decode('utf-8')}"
        )
    except HTTPError as e:
        res = e.read().decode("utf-8")
        if e.status == 500:
            if json.loads(res) == {"results": []}:
                raise Exception(
                    f"API endpoint returned 500 status code, this is most likely due to trying to submit to a sattelite instead of the active master: {res}"
                )
            else:
                raise Exception(f"API endpoint returned 500: {res}")
        elif e.status == 404:
            raise Exception(
                "API endpoint returned 404, this is probably due to your filters not matching (hostname or service name)."
            )


def main():

    # parse command line arguments
    args = parse_args()

    # load or create config file
    config = load_config(args)

    # deliver results to api
    deliver(args, config)


if __name__ == "__main__":
    main()
