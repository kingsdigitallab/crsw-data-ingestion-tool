"""rclone interaction: detection, preflight probes, transfer, verification.

NO printing and NO prompts in this module. Every function returns
structured results or raises TransferError with a .kind that deposit.py
translates into a human-readable message. Raw rclone stderr goes into
TransferError.detail and is shown only in --verbose output — never by
default (spec §8: no raw socket/S3 errors in front of users)."""
import json
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

PROBE_NAME = ".crsw-preflight-probe"


class TransferError(Exception):
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(kind)
        self.kind = kind
        self.detail = detail


def find_rclone(cwd=None) -> Optional[str]:
    """Look for rclone on PATH, then ./rclone, then ./rclone.exe."""
    on_path = shutil.which("rclone")
    if on_path:
        return on_path
    base = Path(cwd) if cwd else Path.cwd()
    for name in ("rclone", "rclone.exe"):
        candidate = base / name
        if candidate.is_file():
            return str(candidate)
    return None


_ERROR_PATTERNS = (
    ("unreachable", ("dial tcp", "no such host", "connection refused",
                     "i/o timeout", "tls handshake", "network is unreachable",
                     "connection reset")),
    ("credentials", ("invalidaccesskeyid", "signaturedoesnotmatch",
                     "401", "unauthorized", "credentials")),
    ("permission", ("accessdenied", "access denied", "403", "forbidden")),
    ("not_found", ("nosuchbucket", "404", "not found", "directory not found")),
)


def classify_error(stderr: str) -> str:
    text = (stderr or "").lower()
    for kind, needles in _ERROR_PATTERNS:
        for needle in needles:
            if needle in text:
                return kind
    return "unknown"


# Fail fast instead of letting rclone retry for minutes: preflight checks
# must answer quickly so the VPN-off case is a message, not a hang.
_FAST_FAIL = ["--retries", "1", "--low-level-retries", "2",
              "--contimeout", "10s"]


def _run(rclone: str, args: List[str],
         timeout: Optional[int] = 30) -> Tuple[int, str, str]:
    """Single choke-point for rclone subprocess calls (tests mock this).
    timeout=None means no cap — required for large uploads. A hung
    subprocess is reported as an i/o timeout so classify_error maps it
    to 'unreachable' instead of raising a stack trace."""
    try:
        proc = subprocess.run(
            [rclone] + args + _FAST_FAIL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        return (124, "", "i/o timeout: rclone did not respond")
    return (proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def remote_names(rclone: str) -> List[str]:
    code, out, err = _run(rclone, ["listremotes"])
    if code != 0:
        raise TransferError("unknown", err)
    return [line.rstrip(":") for line in out.splitlines() if line.strip()]


def check_access(rclone: str, remote: str, bucket: str) -> Optional[str]:
    """None if the bucket is listable; else an error kind."""
    code, out, err = _run(
        rclone, ["lsjson", "--max-depth", "1", "%s:%s" % (remote, bucket)])
    if code == 0:
        return None
    return classify_error(err)


def check_write(rclone: str, remote: str, bucket: str, prefix: str) -> Optional[str]:
    """Probe write permission on the target prefix with touch + deletefile.
    None if writable; else an error kind. Access is scoped by strand, so
    the probe must use the real deposit prefix."""
    probe = "%s:%s/%s/%s" % (remote, bucket, prefix, PROBE_NAME)
    code, out, err = _run(rclone, ["touch", probe])
    if code != 0:
        return classify_error(err)
    _run(rclone, ["deletefile", probe])  # best effort; probe is zero bytes
    return None


def copyto(rclone: str, local_path, remote: str, bucket: str, key: str,
           show_progress: bool = False) -> None:
    """Upload with `rclone copyto` so the object lands at exactly `key`.

    NEVER change this to `rclone copy`: copy treats the destination as a
    directory and nests the original filename inside it (spec §9 — this
    has already caused a real incident)."""
    dest = "%s:%s/%s" % (remote, bucket, key)
    if show_progress:
        # Let rclone draw progress on the user's terminal directly.
        proc = subprocess.run([rclone, "copyto", "--progress",
                               str(local_path), dest])
        if proc.returncode != 0:
            raise TransferError("unknown",
                                "rclone exited %d" % proc.returncode)
        return
    code, out, err = _run(rclone, ["copyto", str(local_path), dest],
                          timeout=None)
    if code != 0:
        raise TransferError(classify_error(err), err)


def key_exists(rclone: str, remote: str, bucket: str, key: str) -> bool:
    """True if an object exists at exactly `key` (rclone lsjson, spec §9)."""
    code, out, err = _run(
        rclone, ["lsjson", "--files-only", "%s:%s/%s" % (remote, bucket, key)])
    if code != 0:
        return False
    try:
        return len(json.loads(out)) > 0
    except ValueError:
        return False


def list_projects(rclone: str, remote: str, bucket: str, strand: str) -> List[str]:
    """Existing project prefixes under a strand. Best-effort: any failure
    returns [] and the caller simply can't offer suggestions."""
    code, out, err = _run(
        rclone, ["lsjson", "--dirs-only", "%s:%s/%s" % (remote, bucket, strand)])
    if code != 0:
        return []
    try:
        return sorted(entry["Name"] for entry in json.loads(out)
                      if entry.get("IsDir"))
    except (ValueError, KeyError):
        return []
