"""File types the shared store refuses, and archives it takes with a
caution.

A program, an installer, a system script, a shortcut or an Office file
with macros is refused, by name (the last extension) and by the file's
opening bytes, so a program called results.csv is caught too. Analysis
code (.py, .R, .do, .ipynb, .sql, .js ...) is allowed. Archives are
allowed and never opened.

These checks are called only where a file enters: the CLI, the web
upload, the bulk route and the promoter's check of a staged deposit.
They are never called from record.validate_record, the schema or
keys.build_key, which also run over records already in the store.

No user I/O. Stdlib only."""
from typing import Dict, FrozenSet, Optional

# How many opening bytes the content test wants. A kernel reads at most
# 256 bytes of a "#!" line; the Windows test needs 64.
SNIFF_BYTES = 512

_KINDS = (
    ("a Windows program",
     "exe dll com scr sys drv ocx cpl efi pif xll"),
    ("an installer",
     "msi msp mst msix msixbundle appx appxbundle appinstaller application "
     "dmg pkg mpkg deb rpm apk ipa appimage"),
    ("a macOS or Linux program",
     "app command so dylib ko run"),
    ("a system script",
     "bat cmd ps1 psm1 psd1 ps1xml vbs vbe jse wsf wsh wsc sct hta msc reg "
     "inf sh bash zsh ksh csh tcsh fish"),
    ("a Java program",
     "jar class jnlp"),
    ("a shortcut",
     "lnk url scf desktop"),
    ("an Office file with macros",
     "docm dotm xlsm xltm xlam xla pptm potm ppsm ppam sldm accde mde ade"),
)

# extension (lower case, no dot) -> the kind, in words a depositor reads.
REFUSED_EXTENSIONS = {}  # type: Dict[str, str]
for _kind, _exts in _KINDS:
    for _ext in _exts.split():
        REFUSED_EXTENSIONS[_ext] = _kind

ARCHIVE_EXTENSIONS = frozenset(
    "zip 7z rar tar gz tgz bz2 tbz tbz2 xz txz zst tzst lz lz4 lzma z cab "
    "iso".split())  # type: FrozenSet[str]

# A "#!" line is refused only when it names one of these: the same rule
# as refusing .sh by name. "#!/usr/bin/env python" and Rscript pass.
SHELL_INTERPRETERS = frozenset(
    "sh bash dash zsh ksh ksh93 mksh pdksh csh tcsh fish ash busybox pwsh "
    "powershell".split())  # type: FrozenSet[str]

# Opening bytes -> kind. CAFEBABE is both a universal Mac binary and a
# Java class file; both are refused, so the wording covers both.
MAGICS = (
    (b"\x7fELF", "a Linux program"),
    (b"\xfe\xed\xfa\xce", "a macOS program"),
    (b"\xce\xfa\xed\xfe", "a macOS program"),
    (b"\xfe\xed\xfa\xcf", "a macOS program"),
    (b"\xcf\xfa\xed\xfe", "a macOS program"),
    (b"\xca\xfe\xba\xbe", "a macOS or Java program"),
    (b"\xbe\xba\xfe\xca", "a macOS or Java program"),
    (b"\xca\xfe\xba\xbf", "a macOS program"),
    (b"\xbf\xba\xfe\xca", "a macOS program"),
)
WINDOWS_MAGIC = b"MZ"
WINDOWS_KIND = "a Windows program"
# A real DOS/PE header has NUL bytes in its first 64; a CSV whose first
# cell is "MZ" (Mozambique) has none.
WINDOWS_HEADER_BYTES = 64
SHELL_KIND = "a system script"


def extension(member: str) -> str:
    """Return the last extension of a member path's file name, lower-cased
    and without the dot; "" when there is none. Trailing dots and spaces
    are dropped first. A leading dot alone is a hidden file, not an
    extension."""
    # Windows ignores trailing dots and spaces: "x.exe." runs as x.exe.
    name = member.rsplit("/", 1)[-1].rstrip(". ")
    dot = name.rfind(".")
    if dot <= 0:
        return ""
    return name[dot + 1:].lower()


def refused_name(member: str) -> Optional[str]:
    """Return why a member is refused for its name, e.g. "a Windows
    program (.exe)", or None. Only the file name counts: a folder called
    tools.exe/ refuses nothing."""
    ext = extension(member)
    kind = REFUSED_EXTENSIONS.get(ext)
    if kind is None:
        return None
    return "%s (.%s)" % (kind, ext)


def is_archive(member: str) -> bool:
    """True if the member is a compressed or archive file."""
    return extension(member) in ARCHIVE_EXTENSIONS


def _shebang_interpreter(head: bytes) -> str:
    """Return the program a "#!" first line asks for, as a bare name; ""
    when the line is not one. "env" is looked through, past its options
    and NAME=value settings."""
    if head[:2] != b"#!":
        return ""
    line = head[2:].split(b"\n", 1)[0].decode("ascii", "replace")
    tokens = line.split()
    if not tokens:
        return ""

    def bare(token):
        return token.replace("\\", "/").rsplit("/", 1)[-1].lower()

    name = bare(tokens[0])
    if name != "env":
        return name
    for token in tokens[1:]:
        if token.startswith("-") or "=" in token:
            continue
        return bare(token)
    return ""


def refused_content(head: bytes) -> Optional[str]:
    """Return why a file is refused for how it starts, e.g. "a Windows
    program", or None. `head` is its first SNIFF_BYTES bytes (fewer for
    a shorter file)."""
    if (head[:2] == WINDOWS_MAGIC
            and b"\x00" in head[2:WINDOWS_HEADER_BYTES]):
        return WINDOWS_KIND
    for magic, kind in MAGICS:
        if head[:len(magic)] == magic:
            return kind
    if _shebang_interpreter(head) in SHELL_INTERPRETERS:
        return SHELL_KIND
    return None


def sniff_file(path) -> Optional[str]:
    """Return refused_content for a local file. An OSError is left to the
    caller."""
    with open(str(path), "rb") as f:
        return refused_content(f.read(SNIFF_BYTES))
