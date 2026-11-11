"""Bounded, no-symlink local file operations for campaign records."""

import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from aftercare.business.errors import BusinessError

MAX_METADATA = 128 * 1024
MAX_PACKAGE = 16 * 1024 * 1024


def fail(code, message):
    raise BusinessError(code, message)


@contextmanager
def directory(path, *, create=False, excluded_identity=None):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        fail("FORMAL_PATH_INVALID", "请使用不含父目录跳转的绝对路径。")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if excluded_identity is not None:
            _exclude_directory(fd, excluded_identity)
        for index, part in enumerate(path.parts[1:], 2):
            if create and index == len(path.parts):
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            if excluded_identity is not None:
                _exclude_directory(fd, excluded_identity)
        yield fd
    finally:
        os.close(fd)


def _exclude_directory(fd, excluded_identity):
    info = os.fstat(fd)
    if (info.st_dev, info.st_ino) == excluded_identity:
        fail("FORMAL_PACKAGE_PATH_FORBIDDEN", "私有候选包须位于工作区外。")


def check_private(info, *, directory_entry=False):
    allowed = {0o500, 0o700} if directory_entry else {0o400, 0o600}
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) not in allowed:
        fail("FORMAL_PACKAGE_PERMISSIONS", "私有候选包的文件所有权或权限不符合要求。")


def _file_state(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_private(fd, name, *, maximum=MAX_METADATA, on_read_failure=None):
    """Read a private file and signal errors after its content read has started."""
    if Path(name).name != name or name in {"", ".", ".."}:
        fail("FORMAL_FILE_INVALID", "候选文件名称不符合要求。")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    reading = False
    try:
        with os.fdopen(os.open(name, flags, dir_fd=fd), "rb") as file:
            initial = os.fstat(file.fileno())
            if (
                not stat.S_ISREG(initial.st_mode)
                or initial.st_nlink != 1
                or initial.st_size > maximum
            ):
                fail("FORMAL_FILE_INVALID", "候选文件类型、链接数或大小不符合要求。")
            check_private(initial)
            reading = True
            data = file.read(maximum + 1)
            final = os.fstat(file.fileno())
            check_private(final)
            if (
                len(data) != initial.st_size
                or final.st_nlink != 1
                or _file_state(initial) != _file_state(final)
            ):
                fail("FORMAL_PACKAGE_CHANGED", "读取期间候选文件已发生变化。")
    except (BusinessError, OSError):
        if reading and on_read_failure is not None:
            on_read_failure()
        raise
    return data


def read(fd, name, *, maximum=MAX_METADATA):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(name, flags, dir_fd=fd), "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            fail("FORMAL_FILE_INVALID", "文件类型或大小不符合要求。")
        data = file.read(maximum + 1)
    if len(data) > maximum:
        fail("FORMAL_FILE_INVALID", "文件超过读取上限。")
    return data


def read_path(path, *, maximum=MAX_METADATA):
    path = Path(path)
    with directory(path.parent) as fd:
        return read(fd, path.name, maximum=maximum)


def json_data(data):
    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("Duplicate object key")
        return result

    def nonfinite(_):
        raise ValueError("Non-finite JSON value")

    return json.loads(data, object_pairs_hook=unique, parse_constant=nonfinite)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode()


def write_once(fd, name, value):
    temporary = f".pending-{uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    handle = os.open(temporary, flags, 0o600, dir_fd=fd)
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(json_bytes(value))
            file.flush()
            os.fsync(file.fileno())
        os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        os.fsync(fd)
    except FileExistsError:
        fail("FORMAL_RECORD_EXISTS", "该登记已经写入，需要保留原记录。")
    finally:
        os.unlink(temporary, dir_fd=fd)


def append_event(fd, value):
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    payload = json_bytes(value)
    with os.fdopen(os.open("exposure.jsonl", flags, 0o600, dir_fd=fd), "ab") as file:
        info = os.fstat(file.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size + len(payload) > MAX_METADATA
        ):
            fail("FORMAL_STATE_INVALID", "暴露登记不可用。")
        check_private(info)
        file.write(payload)
        file.flush()
        os.fsync(file.fileno())
        final = os.fstat(file.fileno())
        check_private(final)
        if final.st_nlink != 1 or final.st_size != info.st_size + len(payload):
            fail("FORMAL_STATE_INVALID", "暴露登记在写入期间已变化。")
    os.fsync(fd)
