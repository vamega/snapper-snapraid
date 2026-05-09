import argparse
import concurrent.futures
import json
import logging
import logging.handlers
import math
import os
import re
import subprocess
import traceback
import sys
from time import gmtime
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple
from datetime import datetime, timedelta
from operator import itemgetter

import requests

from snapper_snapraid.reports.discord_report import create_discord_report
from snapper_snapraid.reports.email_report import create_email_report
from snapper_snapraid.utils import format_delta, human_readable_size

config: Dict = {}

#
# Parse command line args

parser = argparse.ArgumentParser(description="SnapRAID execution wrapper")
parser.add_argument(
    "-f",
    "--force",
    help="Ignore any set thresholds or warnings and execute all jobs regardless",
    action="store_true",
)
parser.add_argument(
    "-c", "--config", help="Path to snapraid-snapper configuration file"
)
parser.add_argument(
    "--disable-systemd", help="Have logs go to stdout/stderr", action="store_true"
)
parser.add_argument(
    "--allow-smart-failure",
    help="Allow the job to complete when SMART data cannot be collected",
    action="store_true",
)

args = parser.parse_args()
force_script_execution = args.force
config_file_path = args.config


# ANSI Color codes
_GREY = "\x1b[38;20m"
_YELLOW = "\x1b[33;20m"
_RED = "\x1b[31;20m"
_BOLD_RED = "\x1b[31;1m"
_RESET = "\x1b[0m"


class ScriptLogFormatter(logging.Formatter):
    def __init__(self, use_color: bool = True):
        _format = f"%(asctime)s #COLOR#[%(levelname)-8s]{_RESET} %(message)s"
        if not use_color:
            _format = f"%(asctime)s [%(levelname)-8s] %(message)s"

        self._formatters = {
            k: logging.Formatter(
                v, datefmt="%Y-%m-%d,%H:%M:%S", defaults={"CMD": "", "STREAM": ""}
            )
            for k, v in {
                logging.DEBUG: _format.replace("#COLOR#", _GREY),
                logging.INFO: _format.replace("#COLOR#", _GREY),
                logging.WARNING: _format.replace("#COLOR#", _YELLOW),
                logging.ERROR: _format.replace("#COLOR#", _RED),
                logging.CRITICAL: _format.replace("#COLOR#", _BOLD_RED),
            }.items()
        }

    def format(self, record):
        formatter = self._formatters.get(record.levelno)
        return formatter.format(record)


class SubprocessFormatter(logging.Formatter):
    def __init__(self, use_color: bool = False):
        _known_fmt_str = (
            "%(asctime)s [%(levelname)-8s][%(CMD)-6.6s]"
            "#COLOR#[%(STREAM)-3.3s] %(message)s#RESET#"
        )
        _unknown_fmt_str = (
            f"%(asctime)s [%(levelname)-8s]$#COLOR#[??????]#RESET#"
            f"#COLOR#[???]#RESET# %(message)s"
        )

        if not use_color:
            _known_fmt_str = (
                "%(asctime)s [%(levelname)-8s][%(CMD)-6.6s][%(STREAM)-3.3s] %(message)s"
            )
            _unknown_fmt_str = (
                f"%(asctime)s [%(levelname)-8s]$[??????][???] %(message)s"
            )

        self._formatters = {
            k: logging.Formatter(v, datefmt="%Y-%m-%d,%H:%M:%S")
            for k, v in {
                "OUT": _known_fmt_str.replace("#COLOR#", _YELLOW).replace(
                    "#RESET#", _RESET
                ),
                "ERR": _known_fmt_str.replace("#COLOR#", _RED).replace(
                    "#RESET#", _RESET
                ),
            }.items()
        }
        self._default_formatter = logging.Formatter(
            _unknown_fmt_str.replace("#COLOR#", _RED).replace("#RESET#", _RESET)
        )

    def format(self, record):
        return self._formatters.get(record.STREAM, self._default_formatter).format(
            record
        )


# Configure logging
# TODO: Maybe worth auto-detecing if running under systemd and when not running
# under systemd log to the console instead?
#
# Can probably check for the INVOCATION_ID environment variable, or perhaps do something
# with the JOURNAL_STREAM environment variable
# Both are described in https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html

logging.root.setLevel(logging.INFO)
logging.Formatter.converter = gmtime

log = logging.getLogger("snapper-snapraid")
log.setLevel(logging.DEBUG)

subprocess_log = logging.getLogger("subprocess")
subprocess_log.setLevel(logging.INFO)

_HAS_SYSTEMD = False
try:
    from systemd.journal import JournalHandler
    from systemd.daemon import notify as sd_notify

    _HAS_SYSTEMD = True
except ImportError:
    pass

if not args.disable_systemd and _HAS_SYSTEMD:
    logging.root.addHandler(JournalHandler())
else:
    if sys.stderr.isatty():
        use_color = True
    else:
        use_color = False

    ch = logging.StreamHandler()
    ch.setFormatter(ScriptLogFormatter(use_color=use_color))

    sh = logging.StreamHandler()
    sh.setFormatter(SubprocessFormatter(use_color=use_color))

    logging.root.addHandler(ch)
    subprocess_log.addHandler(sh)
    subprocess_log.propagate = False

#
# Notification helpers


def notify_and_handle_error(message: str, error: BaseException) -> None:
    log.error(message)
    log.error("".join(traceback.format_exception(None, error, error.__traceback__)))

    send_email("WARNING! SnapRAID jobs unsuccessful", message.replace("\n", "<br>"))
    notify_warning(message)

    exit(1)


def notify_warning(message: str, embeds: Optional[List[Any]] = None) -> Optional[str]:
    return send_discord(f":warning: [**WARNING!**] {message}", embeds=embeds)


def notify_info(
    message: str, embeds: Optional[List[Any]] = None, message_id: Optional[str] = None
) -> Optional[str]:
    if not args.disable_systemd and _HAS_SYSTEMD:
        sd_notify(message)
    return send_discord(
        f":information_source: [**INFO**] {message}", embeds, message_id
    )


def send_discord(
    message: str, embeds: Optional[List[Any]] = None, message_id: Optional[str] = None
) -> Optional[str]:
    is_enabled, webhook_id, webhook_token = itemgetter(
        "enabled", "webhook_id", "webhook_token"
    )(config["notifications"]["discord"])

    if not is_enabled:
        return

    if embeds is None:
        embeds = []

    data = {
        "content": message,
        "embeds": embeds,
        "username": "Snapper",
    }

    update_message = message_id is not None
    base_url = f"https://discord.com/api/webhooks/{webhook_id}/{webhook_token}"

    if update_message:
        discord_url = f"{base_url}/messages/{message_id}"
        response = requests.patch(discord_url, json=data)
    else:
        discord_url = f"{base_url}?wait=true"
        response = requests.post(discord_url, json=data)

    try:
        response.raise_for_status()
        log.debug("Successfully posted message to discord")

        if not update_message:
            data = response.json()

            # Return the message ID in case we want to manipulate it
            return data["id"]
    except requests.exceptions.HTTPError as err:
        # Handle error when trying to update a message
        if update_message:
            log.debug("Failed to update message, posting new.")
            return send_discord(message, embeds=embeds)

        log.error("Unable to send message to discord")
        log.error(str(err))


def send_email(subject: str, message) -> None:
    is_enabled, mail_bin, from_email, to_email = itemgetter(
        "enabled", "binary", "from_email", "to_email"
    )(config["notifications"]["email"])

    if not is_enabled:
        return

    log.debug("Attempting to send email...")

    if not os.path.isfile(mail_bin):
        raise FileNotFoundError("Unable to find mail executable", mail_bin)

    result = subprocess.run(
        [
            mail_bin,
            "-a",
            "Content-Type: text/html",
            "-s",
            subject,
            "-r",
            from_email,
            to_email,
        ],
        input=message,
        capture_output=True,
        text=True,
    )

    if result.stderr:
        raise ConnectionError("Unable to send email", result.stderr)

    log.debug(f"Successfully sent email to {to_email}")


#
# Snapraid Helpers


def spin_down():
    hdparm_bin, is_enabled, drives = itemgetter("binary", "enabled", "drives")(
        config["spindown"]
    )

    if not is_enabled:
        return

    if not os.path.isfile(hdparm_bin):
        raise FileNotFoundError("Unable to find hdparm executable", hdparm_bin)

    log.info(f"Attempting to spin down all {drives} drives...")

    content_files, parity_files = get_snapraid_config()
    drives_to_spin_down = parity_files + (content_files if drives == "all" else [])

    shell_command = (
        f"{hdparm_bin} -y $("
        f'df {" ".join(drives_to_spin_down)} | '  # Get the drives
        f"tail -n +2 | "  # Remove the header
        f'cut -d " " -f1 | '  # Split by space, get the first item
        f'tr "\\n" " "'  # Replace newlines with spaces
        f")"
    )

    try:
        process = subprocess.run(
            shell_command, shell=True, capture_output=True, text=True
        )

        rc = process.returncode

        if rc == 0:
            log.info("Successfully spun down drives.")
        else:
            log.error(
                "Unable to successfully spin down hard drives, see error output below."
            )
            log.error(process.stderr)
            log.error(f"Shell command executed: {shell_command}")
    except Exception as err:
        log.error("Encountered exception while attempting to spin down drives:")
        log.error(str(err))


#
# Snapraid Commands


class SnapraidTag(NamedTuple):
    name: str
    values: Tuple[str, ...]
    raw: str


SNAPRAID_STRUCTURED_LOG_ARGS = ["--gui", "--log", ">&2"]
DIFF_KEYS = [
    "equal",
    "added",
    "removed",
    "updated",
    "moved",
    "copied",
    "relocated",
    "restored",
]
GIGA = 1000**3
MEBI = 1024**2
SMARTCTL_FLAG_UNSUPPORTED = 1 << 0
SMARTCTL_FLAG_OPEN = 1 << 1
SMARTCTL_FLAG_FAIL = 1 << 3
SMARTCTL_FLAG_PREFAIL = 1 << 4
SMARTCTL_FLAG_PREFAIL_LOGGED = 1 << 5
SMARTCTL_FLAG_ERROR_LOGGED = 1 << 6
SMARTCTL_FLAG_SELFERROR_LOGGED = 1 << 7
SMART_FAILING_FLAGS = SMARTCTL_FLAG_FAIL | SMARTCTL_FLAG_PREFAIL


def parse_snapraid_tag_line(line: str) -> Optional[SnapraidTag]:
    parts = []
    part = []
    i = 0

    while i < len(line):
        char = line[i]

        if char == ":":
            parts.append("".join(part))
            part = []
        elif char == "\\":
            i += 1

            if i >= len(line):
                part.append("\\")
            elif line[i] == "d":
                part.append(":")
            elif line[i] == "n":
                part.append("\n")
            elif line[i] == "r":
                part.append("\r")
            elif line[i] == "\\":
                part.append("\\")
            else:
                part.append("\\")
                part.append(line[i])
        else:
            part.append(char)

        i += 1

    parts.append("".join(part))

    if len(parts) < 2 or not re.match(r"^[a-z][a-z0-9_]*$", parts[0]):
        return None

    return SnapraidTag(parts[0], tuple(parts[1:]), line)


def parse_snapraid_tags(data: str) -> List[SnapraidTag]:
    tags = []

    for line in data.splitlines():
        tag = parse_snapraid_tag_line(line)

        if tag is not None:
            tags.append(tag)

    return tags


def _format_gb(size_bytes: int, precision: int = 0) -> str:
    value = size_bytes / GIGA

    if precision == 0:
        return str(int(value))

    return f"{value:.{precision}f}"


def _format_probability_percent(probability: float) -> str:
    return str(int(probability * 100 + 0.5))


def _int_or_default(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _log_structured_snapraid_tag(tag: SnapraidTag, cmd: str) -> None:
    if tag.name == "msg" and len(tag.values) >= 2:
        level = tag.values[0]
        message = tag.values[1]

        if level in ["fatal", "fatal_hardware", "error", "error_hardware"]:
            subprocess_log.error(message, extra={"STREAM": "ERR", "CMD": cmd})
        elif level == "verbose":
            subprocess_log.debug(message, extra={"STREAM": "ERR", "CMD": cmd})
        else:
            subprocess_log.info(message, extra={"STREAM": "ERR", "CMD": cmd})
    else:
        subprocess_log.debug(tag.raw, extra={"STREAM": "ERR", "CMD": cmd})


def run_snapraid(
    commands,
    progress_handler: Optional[Callable[[str], Any]] = None,
    allowed_return_codes=None,
):
    snapraid_bin, snapraid_config = itemgetter("binary", "config")(config["snapraid"])

    if not os.path.isfile(snapraid_bin):
        raise FileNotFoundError("Unable to find SnapRAID executable", snapraid_bin)

    if allowed_return_codes is None:
        allowed_return_codes = []

    std_out = []
    std_err = []
    snapraid_commands = (
        [snapraid_bin, "--conf", snapraid_config]
        + commands
        + SNAPRAID_STRUCTURED_LOG_ARGS
    )

    with (
        subprocess.Popen(
            snapraid_commands,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        ) as process,
        concurrent.futures.ThreadPoolExecutor(2) as tpe,
    ):

        def read_stdout(file):
            _cmd = " ".join(commands)
            for line in file:
                rline = line.rstrip()

                if progress_handler is not None and progress_handler(rline):
                    continue

                subprocess_log.info(rline, extra={"STREAM": "OUT", "CMD": _cmd})
                std_out.append(rline)

        def read_stderr(file):
            _cmd = " ".join(commands)
            for line in file:
                rline = line.rstrip()

                if progress_handler is not None and progress_handler(rline):
                    continue

                tag = parse_snapraid_tag_line(rline)

                if tag is None:
                    subprocess_log.error(rline, extra={"STREAM": "ERR", "CMD": _cmd})
                else:
                    _log_structured_snapraid_tag(tag, _cmd)

                std_err.append(rline)

        f1 = tpe.submit(read_stdout, process.stdout)
        f2 = tpe.submit(read_stderr, process.stderr)
        f1.result()
        f2.result()

    rc = process.returncode

    if not (rc == 0 or rc in allowed_return_codes):
        last_lines = "\n".join(std_err[-10:])

        raise SystemError(
            f"A critical SnapRAID error was encountered during command "
            f'`snapraid {" ".join(commands)}`. The process exited with code `{rc}`.\n'
            f"Here are the last **10 lines** from the error log:\n```\n"
            f"{last_lines}\n```\nThis requires your immediate attention.",
            "\n".join(std_err),
            "\n".join(std_out),
        )

    return "\n".join(std_out), "\n".join(std_err)


def parse_status_tags(tags: List[SnapraidTag]):
    summary: Dict[str, List[str]] = {}
    content_info: Dict[str, int] = {}
    disk_stats: Dict[str, Dict[str, int]] = {}
    block_count = 0

    disk_mapping = {
        "disk_file_count": "file_count",
        "disk_fragmented_file_count": "fragmented_files",
        "disk_excess_fragment_count": "excess_fragments",
        "disk_space_wasted": "wasted",
        "disk_used": "used",
        "disk_free": "free",
        "disk_use_percent": "use_percent",
    }

    for tag in tags:
        if tag.name == "summary" and len(tag.values) >= 2:
            key = tag.values[0]

            if key in disk_mapping and len(tag.values) >= 3:
                disk_name = tag.values[1]
                disk_stats.setdefault(disk_name, {})[disk_mapping[key]] = int(
                    tag.values[2]
                )
            else:
                summary[key] = list(tag.values[1:])
        elif tag.name == "content_info" and len(tag.values) == 2:
            content_info[tag.values[0]] = int(tag.values[1])
        elif tag.name == "block_count" and tag.values:
            block_count = int(tag.values[0])

    required_summary_keys = [
        "scrub_oldest_days",
        "scrub_median_days",
        "scrub_newest_days",
    ]

    if not all(k in summary for k in required_summary_keys):
        raise ValueError("Unable to parse SnapRAID status structured output")

    block_count = content_info.get("block", block_count)
    unscrubbed_blocks = content_info.get("block_unscrubbed", 0)
    unscrubbed_percent = (
        0 if block_count == 0 else math.ceil(unscrubbed_blocks * 100 / block_count)
    )

    error_count = content_info.get("block_bad", 0)
    if error_count == 0 and "has_bad" in summary:
        error_count = _int_or_default(summary["has_bad"][0])

    sync_in_progress = content_info.get("block_unsynced", 0) > 0
    if not sync_in_progress:
        sync_in_progress = summary.get("exit", [""])[0] == "unsynced"
    if not sync_in_progress and "has_unsynced" in summary:
        sync_in_progress = _int_or_default(summary["has_unsynced"][0]) > 0

    drive_stats = []
    for disk_name, disk_data in disk_stats.items():
        wasted = disk_data.get("wasted", 0)
        wasted_gb = "-" if wasted < -100 * GIGA else _format_gb(wasted, 1)

        drive_stats.append(
            {
                "drive_name": disk_name,
                "file_count": str(disk_data.get("file_count", 0)),
                "fragmented_files": str(disk_data.get("fragmented_files", 0)),
                "excess_fragments": str(disk_data.get("excess_fragments", 0)),
                "wasted_gb": wasted_gb,
                "used_gb": _format_gb(disk_data.get("used", 0)),
                "free_gb": _format_gb(disk_data.get("free", 0)),
                "use_percent": str(disk_data.get("use_percent", 0)),
            }
        )

    if "file_count" in summary:
        drive_stats.append(
            {
                "drive_name": None,
                "file_count": summary.get("file_count", ["0"])[0],
                "fragmented_files": summary.get("fragmented_file_count", ["0"])[0],
                "excess_fragments": summary.get("excess_fragment_count", ["0"])[0],
                "wasted_gb": _format_gb(
                    _int_or_default(summary.get("total_wasted", ["0"])[0]), 1
                ),
                "used_gb": _format_gb(
                    _int_or_default(summary.get("total_used", ["0"])[0])
                ),
                "free_gb": _format_gb(
                    _int_or_default(summary.get("total_free", ["0"])[0])
                ),
                "use_percent": summary.get("total_use_percent", ["0"])[0],
            }
        )

    return (
        drive_stats,
        {
            "unscrubbed": unscrubbed_percent,
            "scrub_age": int(summary["scrub_oldest_days"][0]),
            "median": int(summary["scrub_median_days"][0]),
            "newest": int(summary["scrub_newest_days"][0]),
        },
        error_count,
        _int_or_default(summary.get("zerosubsecond_file_count", ["0"])[0]),
        sync_in_progress,
    )


def get_status():
    _, snapraid_log = run_snapraid(["status"])

    return parse_status_tags(parse_snapraid_tags(snapraid_log))


def parse_diff_tags(tags: List[SnapraidTag]):
    diff_int = {}

    for tag in tags:
        if (
            tag.name == "summary"
            and len(tag.values) >= 2
            and tag.values[0] in DIFF_KEYS
        ):
            diff_int[tag.values[0]] = int(tag.values[1])

    if not diff_int:
        raise ValueError("Unable to parse diff output from SnapRAID, not proceeding.")

    for key in DIFF_KEYS:
        diff_int.setdefault(key, 0)

    return diff_int


def parse_diff_output(snapraid_diff: str):
    return parse_diff_tags(parse_snapraid_tags(snapraid_diff))


def should_rerun_sync_from_tags(tags: List[SnapraidTag]) -> bool:
    summary = {}

    for tag in tags:
        if tag.name == "summary" and len(tag.values) >= 2:
            summary[tag.values[0]] = tag.values[1]

    return (
        summary.get("exit") == "warning"
        and _int_or_default(summary.get("error_soft")) > 0
        and _int_or_default(summary.get("error_io")) == 0
        and _int_or_default(summary.get("error_data")) == 0
    )


def get_diff():
    snapraid_diff, snapraid_log = run_snapraid(["diff"], allowed_return_codes=[2])

    return parse_diff_output("\n".join([snapraid_log, snapraid_diff]))


def smart_tags_have_failing_disk(tags: List[SnapraidTag]) -> bool:
    for tag in tags:
        if tag.name != "attr" or len(tag.values) < 4:
            continue

        attr_name = tag.values[2]

        if attr_name == "flags" and _int_or_default(tag.values[3]) & SMART_FAILING_FLAGS:
            return True

        if attr_name.isdigit() and len(tag.values) >= 12 and tag.values[11] == "now":
            return True

    return False


def parse_smart_tags(tags: List[SnapraidTag]):
    devices: Dict[Tuple[str, str], Dict[str, Any]] = {}
    global_fp = None

    def device_entry(device: str, disk: str) -> Dict[str, Any]:
        return devices.setdefault(
            (device, disk),
            {
                "temp": "-",
                "power_on_days": "-",
                "error_count": "-",
                "fp": "-",
                "size": "-",
                "serial": "-",
                "device": device if device else "-",
                "disk": disk if disk else "-",
                "_error_protocol": 0,
                "_error_medium": 0,
                "_flags": 0,
                "_rotationrate": None,
                "_failure_probability": None,
            },
        )

    for tag in tags:
        if tag.name in ["smart", "info"] and len(tag.values) >= 2:
            device_entry(tag.values[0], tag.values[1])
        elif tag.name == "attr" and len(tag.values) >= 4:
            device, disk, attr_name = tag.values[0], tag.values[1], tag.values[2]
            attr_values = tag.values[3:]
            entry = device_entry(device, disk)

            if attr_name == "serial":
                entry["serial"] = attr_values[0] if attr_values[0] else "-"
            elif attr_name == "size":
                entry["size"] = f"{int(attr_values[0]) / 1E12:.1f}"
            elif attr_name == "temperature":
                entry["temp"] = attr_values[0]
            elif attr_name == "rotationrate":
                entry["_rotationrate"] = _int_or_default(attr_values[0])
            elif attr_name == "error_protocol":
                entry["_error_protocol"] = _int_or_default(attr_values[0])
            elif attr_name == "error_medium":
                entry["_error_medium"] = _int_or_default(attr_values[0])
            elif attr_name == "flags":
                entry["_flags"] = _int_or_default(attr_values[0])
            elif attr_name == "afr" and len(attr_values) >= 2:
                entry["_failure_probability"] = float(attr_values[1])
            elif attr_name == "9":
                entry["power_on_days"] = str(
                    (_int_or_default(attr_values[0]) & 0xFFFFFFFF) // 24
                )
        elif (
            tag.name == "summary"
            and len(tag.values) >= 3
            and tag.values[0] == "array_failure"
        ):
            global_fp = _format_probability_percent(float(tag.values[2]))

    if not devices or global_fp is None:
        raise ValueError(
            "Unable to parse drive data or global failure percentage, not proceeding."
        )

    drive_data = []
    for entry in devices.values():
        flags = entry["_flags"]
        error_count = entry["_error_protocol"] + entry["_error_medium"]

        if flags & SMARTCTL_FLAG_FAIL:
            entry["error_count"] = "FAIL"
        elif flags & SMARTCTL_FLAG_PREFAIL:
            entry["error_count"] = "PREFAIL"
        elif flags & SMARTCTL_FLAG_PREFAIL_LOGGED:
            entry["error_count"] = "logfail"
        elif error_count:
            entry["error_count"] = str(error_count)
        elif flags & SMARTCTL_FLAG_ERROR_LOGGED:
            entry["error_count"] = "logerr"
        elif flags & SMARTCTL_FLAG_SELFERROR_LOGGED:
            entry["error_count"] = "selferr"

        if flags & (SMARTCTL_FLAG_UNSUPPORTED | SMARTCTL_FLAG_OPEN):
            entry["fp"] = "n/a"
        elif entry["_rotationrate"] == 0:
            entry["fp"] = "SSD"
        elif entry["_failure_probability"] is not None:
            entry["fp"] = f'{_format_probability_percent(entry["_failure_probability"])}%'

        drive_data.append(
            {k: v for k, v in entry.items() if not k.startswith("_")}
        )

    return drive_data, global_fp


def _allow_smart_failure(error: BaseException):
    log.warning(
        "SMART data could not be collected, continuing because "
        "--allow-smart-failure was set."
    )
    log.warning(str(error))
    notify_warning(
        "SMART data could not be collected, continuing because "
        "`--allow-smart-failure` was set."
    )

    return [], "-"


def get_smart(allow_failure: bool = False):
    try:
        smart_data, smart_log = run_snapraid(["smart"])
    except SystemError as err:
        smart_log = err.args[1] if len(err.args) > 1 else ""
        smart_stdout = err.args[2] if len(err.args) > 2 else ""
        tags = parse_snapraid_tags("\n".join([smart_log, smart_stdout]))

        if allow_failure and not smart_tags_have_failing_disk(tags):
            return _allow_smart_failure(err)

        raise err

    tags = parse_snapraid_tags("\n".join([smart_log, smart_data]))

    try:
        return parse_smart_tags(tags)
    except ValueError as err:
        if allow_failure and not smart_tags_have_failing_disk(tags):
            return _allow_smart_failure(err)

        raise err


def handle_progress():
    start = datetime.now()
    message_id = None

    def handler(data):
        nonlocal start
        nonlocal message_id

        def send_progress_message(msg: str) -> None:
            nonlocal start
            nonlocal message_id

            if datetime.now() - start < timedelta(minutes=1):
                return

            if message_id is None:
                message_id = notify_info(msg)
            else:
                new_message_id = notify_info(msg, message_id=message_id)

                if new_message_id:
                    message_id = new_message_id

            start = datetime.now()

        tag = parse_snapraid_tag_line(data)

        if tag is not None and tag.name == "run":
            if not tag.values:
                return True

            if tag.values[0] != "pos":
                return tag.values[0] in ["begin", "end"]

            if len(tag.values) < 11:
                return True

            size_done_mb = _int_or_default(tag.values[3]) // MEBI
            msg = (
                f"Current progress **{tag.values[4]}%** "
                f"(`{human_readable_size(size_done_mb)}`)"
            )

            if tag.values[6] and tag.values[7]:
                msg = (
                    f"{msg} — processing at **{int(tag.values[6]):,} MB/s** "
                    f"(*{tag.values[7]}% CPU*)."
                )

                if tag.values[5]:
                    eta_seconds = int(tag.values[5])
                    eta_minutes = eta_seconds // 60
                    msg = (
                        f"{msg} **ETA:** {eta_minutes // 60}h "
                        f"{eta_minutes % 60}m"
                    )

            send_progress_message(msg)

            return True

        return False

    return handler


def _run_sync(run_count: int):
    pre_hash, auto_sync = itemgetter("pre_hash", "auto_sync")(
        config["snapraid"]["sync"]
    )
    auto_sync_enabled, max_attempts = itemgetter("enabled", "max_attempts")(auto_sync)

    try:
        log.info(
            f"Running SnapRAID sync ({run_count}) "
            f'{"with" if pre_hash else "without"} pre-hashing...'
        )
        notify_info(f"Syncing **({run_count})**...")

        run_snapraid(["sync", "-h"] if pre_hash else ["sync"], handle_progress())
    except SystemError as err:
        sync_errors = err.args[1]

        if sync_errors is None:
            raise err

        sync_error_tags = parse_snapraid_tags(sync_errors)
        should_rerun = should_rerun_sync_from_tags(sync_error_tags)

        if should_rerun:
            log.info(
                "SnapRAID has indicated another sync is recommended, due to disks or files being "
                "modified during the sync process."
            )

        if should_rerun and auto_sync_enabled and run_count < max_attempts:
            log.info("Re-running sync command with identical options...")
            _run_sync(run_count + 1)
        else:
            raise err


def run_sync() -> str:
    start = datetime.now()
    _run_sync(1)
    end = datetime.now()

    sync_job_time = format_delta(end - start)

    log.info(f"Sync job finished, elapsed time {sync_job_time}")
    notify_info(f"Sync job finished, elapsed time **{sync_job_time}**")

    return sync_job_time


def run_scrub() -> Optional[str]:
    snapraid_scrub_config = config["snapraid"]["scrub"]
    enabled = snapraid_scrub_config["enabled"]
    scrub_new = snapraid_scrub_config["scrub_new"]
    check_percent = snapraid_scrub_config["check_percent"]
    min_age = snapraid_scrub_config["min_age"]

    if not enabled:
        log.info("Scrubbing not enabled, skipping.")

        return None

    log.info("Running scrub job...")

    start = datetime.now()

    if scrub_new:
        log.info("Scrubbing new blocks...")
        notify_info("Scrubbing new blocks...")

        scrub_new_output, _ = run_snapraid(["scrub", "-p", "new"], handle_progress())

    log.info("Scrubbing old blocks...")
    notify_info("Scrubbing old blocks...")

    scrub_output, _ = run_snapraid(
        ["scrub", "-p", str(check_percent), "-o", str(min_age)], handle_progress()
    )

    end = datetime.now()

    scrub_job_time = format_delta(end - start)

    log.info(f"Scrub job finished, elapsed time {scrub_job_time}")
    notify_info(f"Scrub job finished, elapsed time **{scrub_job_time}**")

    return scrub_job_time


def run_touch() -> None:
    run_snapraid(["touch"])


#
# Sanity Checker


def get_snapraid_config() -> Tuple[List[str], List[str]]:
    config_file = config["snapraid"]["config"]

    if not os.path.isfile(config_file):
        raise FileNotFoundError("Unable to find SnapRAID configuration", config_file)

    with open(config_file, "r") as file:
        snapraid_config = file.read()

    file_regex = re.compile(
        r"^(content|parity) +(.+/\w+.(?:content|parity)) *$", flags=re.MULTILINE
    )
    parity_files = []
    content_files = []

    for m in file_regex.finditer(snapraid_config):
        if m[1] == "content":
            content_files.append(m[2])
        else:
            parity_files.append(m[2])

    return content_files, parity_files


def sanity_check() -> None:
    content_files, parity_files = get_snapraid_config()
    files = content_files + parity_files

    for file in files:
        if not os.path.isfile(file):
            raise FileNotFoundError(
                "Unable to locate required content/parity file", file
            )

    log.info(f"All {len(files)} content and parity files found, proceeding.")


#
# Main


def main():
    try:
        total_start = datetime.now()

        log.info("Snapper started")
        notify_info("Starting SnapRAID jobs...")

        log.info("Running sanity checks...")

        sanity_check()

        log.info("Checking for errors and files with zero sub-second timestamps...")

        (_, _, error_count, zero_subsecond_count, sync_in_progress) = get_status()

        if error_count > 0:
            if force_script_execution:
                log.error(
                    f"There are {error_count} error(s) in the array, "
                    f"ignoring due to forced run."
                )
                notify_warning(
                    f"There are **{error_count}** error(s) in the array, "
                    f"ignoring due to forced run."
                )
            else:
                raise SystemError(
                    f"There are {error_count} error(s) in the array, you should review "
                    f"this immediately. All jobs have been halted."
                )

        if zero_subsecond_count > 0:
            log.info(
                f"Found {zero_subsecond_count} file(s) with zero sub-second timestamp"
            )
            log.info("Running touch job...")
            run_touch()

        log.info("Get SnapRAID diff...")

        diff_data = get_diff()

        log.info(
            f'Diff output: {diff_data["equal"]} equal, '
            + f'{diff_data["added"]} added, '
            + f'{diff_data["removed"]} removed, '
            + f'{diff_data["updated"]} updated, '
            + f'{diff_data["moved"]} moved, '
            + f'{diff_data["copied"]} copied, '
            + f'{diff_data["relocated"]} relocated, '
            + f'{diff_data["restored"]} restored'
        )

        if (
            sum(diff_data.values()) - diff_data["equal"] > 0
            or sync_in_progress
            or force_script_execution
        ):
            thresholds_conf = config["snapraid"]["diff"]["thresholds"]
            updated_threshold = thresholds_conf["updated"]
            removed_threshold = thresholds_conf["removed"]

            if force_script_execution:
                log.info("Ignoring any thresholds and forcefully proceeding with sync.")
            elif 0 < updated_threshold < diff_data["updated"]:
                raise ValueError(
                    f'More files ({diff_data["updated"]}) have been updated than the '
                    f"configured max ({updated_threshold})"
                )
            elif 0 < removed_threshold < diff_data["removed"]:
                raise ValueError(
                    f'More files ({diff_data["removed"]}) have been removed than the configured '
                    f"max ({removed_threshold})"
                )
            elif sync_in_progress:
                log.info("A previous sync in progress has been detected, resuming.")
            else:
                if updated_threshold > 0:
                    log.info(
                        f'Fewer files updated ({diff_data["updated"]}) than the configured '
                        f"limit ({updated_threshold}), proceeding."
                    )
                if removed_threshold > 0:
                    log.info(
                        f'Fewer files removed ({diff_data["removed"]}) than the configured '
                        f"limit ({removed_threshold}), proceeding."
                    )

            sync_job_time = run_sync()
            sync_job_ran = True
        else:
            log.info("No changes to sync, skipping.")
            notify_info("No changes to sync")

            sync_job_ran = False
            sync_job_time = None

        scrub_job_time = run_scrub()
        scrub_job_ran = scrub_job_time is not None

        log.info("Fetching SnapRAID status...")
        (drive_stats, scrub_stats, error_count, _, _) = get_status()

        log.info(
            f'{scrub_stats["unscrubbed"]}% of the array has not been scrubbed, with the '
            f'oldest block at {scrub_stats["scrub_age"]} day(s), the '
            f'median at {scrub_stats["median"]} day(s), and the newest at '
            f'{scrub_stats["newest"]} day(s).'
        )

        log.info("Fetching smart data...")
        (smart_drive_data, global_fp) = get_smart(
            allow_failure=args.allow_smart_failure
        )

        if global_fp == "-":
            log.warning("Drive failure probability this year is unavailable")
        else:
            log.info(f"Drive failure probability this year is {global_fp}%")

        total_time = format_delta(datetime.now() - total_start)

        report_data = {
            "sync_job_ran": sync_job_ran,
            "scrub_job_ran": scrub_job_ran,
            "sync_job_time": sync_job_time,
            "scrub_job_time": scrub_job_time,
            "diff_data": diff_data,
            "zero_subsecond_count": zero_subsecond_count,
            "scrub_stats": scrub_stats,
            "drive_stats": drive_stats,
            "smart_drive_data": smart_drive_data,
            "global_fp": global_fp,
            "total_time": total_time,
        }

        email_report = create_email_report(report_data)

        send_email("SnapRAID Job Completed Successfully", email_report)

        if config["notifications"]["discord"]["enabled"]:
            (discord_message, embeds) = create_discord_report(report_data)

            send_discord(discord_message, embeds)

        spin_down()

        log.info("SnapRAID jobs completed successfully, exiting.")
    except (ValueError, ChildProcessError, SystemError) as err:
        notify_and_handle_error(err.args[0], err)
    except ConnectionError as err:
        log.error(str(err))
    except FileNotFoundError as err:
        notify_and_handle_error(
            f"{err.args[0]} - missing file path `{err.args[1]}`", err
        )
    except BaseException as err:
        notify_and_handle_error(
            f'Unhandled Python Exception `{str(err) if str(err) else "unknown error"}`',
            err,
        )


def entry_point():
    global config
    with open(config_file_path, "r") as f:
        config = json.load(f)
    main()


if __name__ == "__main__":
    entry_point()
