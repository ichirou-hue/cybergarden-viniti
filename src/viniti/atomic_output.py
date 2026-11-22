"""Publish a same-filesystem directory without replacing a concurrent output."""

import ctypes
import errno
import os
from pathlib import Path
import sys


def publish_directory(source, destination):
    source, destination = Path(source), Path(destination)
    if os.name == "nt":
        # Windows rename rejects an existing target, including empty directories.
        return source.rename(destination)
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = getattr(library, "renamex_np", None)
        arguments = (os.fsencode(source), os.fsencode(destination), 0x00000004)  # RENAME_EXCL
        types = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    elif sys.platform.startswith("linux"):
        function = getattr(library, "renameat2", None)
        arguments = (-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # AT_FDCWD, RENAME_NOREPLACE
        types = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    else:
        function = None
    if function is None:
        raise OSError(errno.ENOSYS, "Atomic no-replace directory publication unavailable.")
    function.argtypes, function.restype = types, ctypes.c_int
    if function(*arguments):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))
    return destination
