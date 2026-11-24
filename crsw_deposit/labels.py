"""Object labels: the four pieces of metadata every uploaded object
carries, members and the dataset record alike.

The names here have no transport prefix. rclone wants
"x-amz-meta-<name>" in its --header-upload flag; boto3 takes the bare
name in Metadata= and adds the prefix itself. Each caller applies its
own rule. The names are defined only here, so the two routes can never
disagree about them.

No user I/O. Stdlib only."""
from typing import Dict

DATASET_UUID = "dataset-uuid"
CHECKSUM = "checksum-sha256"
SENSITIVITY = "sensitivity"
DEPOSITOR = "depositor"

LABEL_NAMES = (DATASET_UUID, CHECKSUM, SENSITIVITY, DEPOSITOR)

S3_META_PREFIX = "x-amz-meta-"


def object_labels(dataset_uuid: str, checksum_sha256: str,
                  sensitivity: str, depositor: str) -> Dict[str, str]:
    """Return the four labels for one object, bare names, in a fixed order."""
    return {
        DATASET_UUID: dataset_uuid,
        CHECKSUM: checksum_sha256,
        SENSITIVITY: sensitivity,
        DEPOSITOR: depositor,
    }


def as_s3_headers(labels: Dict[str, str]) -> Dict[str, str]:
    """Return the labels with the x-amz-meta- prefix spelled out, for
    transports that want it (rclone --header-upload)."""
    return {S3_META_PREFIX + name: value for name, value in labels.items()}
