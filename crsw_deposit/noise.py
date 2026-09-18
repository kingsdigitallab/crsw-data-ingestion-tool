"""OS and editor noise that should not be deposited unless asked (r7 §2).

Excluded by default when a whole folder is walked - and never silently:
callers report how many they dropped and offer a way to include them.
"._*" is a Mac writing to exFAT/SMB: an AppleDouble sidecar for every
single file, invisible in Finder. Browser folder pickers hand back the
same files, so the web route applies the same rules.

No user I/O. Stdlib only."""

NOISE_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
NOISE_FILE_PREFIXES = ("._",)
NOISE_DIR_NAMES = frozenset({
    ".git", "__pycache__", ".ipynb_checkpoints", "__MACOSX",
    ".Spotlight-V100", ".Trashes", ".svn", "$RECYCLE.BIN"})


def is_noise_file(name: str) -> bool:
    """True for a bare filename (one path segment) that is OS noise."""
    return name in NOISE_FILE_NAMES or any(
        name.startswith(p) for p in NOISE_FILE_PREFIXES)


def is_noise_dir(name: str) -> bool:
    """True for a bare directory name that is OS/tool noise."""
    return name in NOISE_DIR_NAMES


def is_noise_member(member: str) -> bool:
    """True if any segment of a '/'-joined member path is noise - a file
    inside a noise directory counts, as does a noise file at any depth.
    For the web route, which receives already-relative paths rather
    than walking a filesystem."""
    parts = member.split("/")
    return any(is_noise_dir(d) for d in parts[:-1]) or is_noise_file(parts[-1])
