"""OS and editor noise that is left out of a deposit unless asked for.

Left out by default when a whole folder is walked, and never silently:
callers report how many they dropped and offer a way to include them.
The web route applies the same rules to the paths a browser folder
picker hands back.

No user I/O. Stdlib only."""

NOISE_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
# "._*" is the AppleDouble sidecar a Mac writes beside every file on
# exFAT or SMB, invisible in Finder.
NOISE_FILE_PREFIXES = ("._",)
NOISE_DIR_NAMES = frozenset({
    ".git", "__pycache__", ".ipynb_checkpoints", "__MACOSX",
    ".Spotlight-V100", ".Trashes", ".svn", "$RECYCLE.BIN"})


def is_noise_file(name: str) -> bool:
    """True if a bare filename (one path segment) is OS noise."""
    return name in NOISE_FILE_NAMES or any(
        name.startswith(p) for p in NOISE_FILE_PREFIXES)


def is_noise_dir(name: str) -> bool:
    """True if a bare directory name is OS or tool noise."""
    return name in NOISE_DIR_NAMES


def is_noise_member(member: str) -> bool:
    """True if any segment of a '/'-joined member path is noise: a noise
    file at any depth, or any file inside a noise directory."""
    parts = member.split("/")
    return any(is_noise_dir(d) for d in parts[:-1]) or is_noise_file(parts[-1])
