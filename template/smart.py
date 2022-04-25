import os
import io
import typing
from collections import defaultdict
from functools import partial
from itertools import chain
from pathlib import PurePath
from urllib.parse import urlsplit

from megfile.errors import ProtocolNotFoundError, UnsupportedError
from megfile.fs import fs_copy, fs_rename, fs_getsize, fs_glob, fs_iglob, fs_glob_stat, fs_path_join
from megfile.interfaces import FileEntry, PathLike, Access, NullCacher
from megfile.lib.combine_reader import CombineReader
from megfile.lib.compat import fspath
from megfile.lib.glob import globlize, ungloblize
from megfile.s3 import is_s3, s3_copy, s3_download, s3_upload, s3_rename, S3Cacher, s3_load_content, s3_glob, s3_iglob, s3_glob_stat, s3_path_join
from megfile.utils import combine, get_content_offset
import megfile

_copy_funcs = {
    's3': {
        's3': s3_copy,
        'fs': s3_download
    },
    'fs': {
        's3': s3_upload,
        'fs': fs_copy,
    }
}


# auto-smart-ignore-start
def smart_islink(*args, **kwargs):
    pass


def smart_open(*args, **kwargs):
    pass


def smart_scan(*args, **kwargs):
    pass


def smart_path_join(*args, **kwargs):
    pass


def smart_remove(*args, **kwargs):
    pass

def smart_unlink(*args, **kwargs):
    pass

def smart_isdir(*args, **kwargs):
    pass
# auto-smart-ignore-end


def register_copy_func(
        src_protocol: str,
        dst_protocol: str,
        copy_func: typing.Optional[typing.Callable[
            [str, str, typing.Optional[typing.
                                       Callable[[int], None]]], None]] = None,
) -> None:
    '''
    Used to register copy func between protocols, and do not allow duplicate registration

    :param src_protocol: protocol name of source file, e.g. 's3'
    :param dst_protocol: protocol name of destination file, e.g. 's3'
    :param copy_func: copy func, its type is: typing.Callable[[str, str, typing.Optional[typing.Callable[[int], None]]], None]
    '''
    try:
        _copy_funcs[src_protocol][dst_protocol]
    except KeyError:
        dst_dict = _copy_funcs.get(src_protocol, {})
        dst_dict[dst_protocol] = copy_func
        _copy_funcs[src_protocol] = dst_dict
    except Exception as error:  # pragma: no cover
        raise error
    else:
        raise ValueError(
            'Copy Function has already existed: {}->{}'.format(
                src_protocol, dst_protocol))


def _default_copy_func(
        src_path: PathLike,
        dst_path: PathLike,
        callback: typing.Optional[typing.Callable[[int], None]] = None) -> None:
    with smart_open(src_path, 'rb') as fsrc:
        with smart_open(dst_path, 'wb') as fdst:
            # This magic number is copied from  copyfileobj
            length = 16 * 1024
            while True:
                buf = fsrc.read(length)  # type: ignore
                if not buf:
                    break
                fdst.write(buf)  # type: ignore
                if callback is None:
                    continue
                callback(len(buf))


def _extract_protocol(path: typing.Union[PathLike, int]
                     ) -> str:
    if isinstance(path, int):
        protocol = "fs"
    elif isinstance(path, str):
        protocol = urlsplit(path).scheme
        if protocol == "":
            protocol = "fs"
    elif isinstance(path, PurePath):
        protocol = "fs"
    else:
        raise ProtocolNotFoundError('protocol not found: %r' % path)
    if protocol == 'https':
        protocol = 'http'
    return protocol


def get_traditional_path(path: PathLike):
    return fspath(path)


def smart_rename(
        src_path: PathLike, dst_path: PathLike,
        followlinks: bool = False) -> None:
    '''
    Move file on s3 or fs. `s3://` or `s3://bucket` is not allowed to move

    :param src_path: Given source path
    :param dst_path: Given destination path
    '''
    if smart_isdir(src_path, followlinks=followlinks):
        raise IsADirectoryError('%r is a directory' % PathLike)
    src_protocol = _extract_protocol(src_path)
    dst_protocol = _extract_protocol(dst_path)
    if src_protocol == dst_protocol:
        if src_protocol == 'fs':
            fs_rename(src_path, dst_path)
        elif src_protocol == 's3':
            s3_rename(src_path, dst_path)
        else:
            raise UnsupportedError(operation='smart_rename', path=src_path)  # pragma: no cover
        return
    smart_copy(src_path, dst_path)
    smart_unlink(src_path)


def smart_copy(
        src_path: PathLike,
        dst_path: PathLike,
        callback: typing.Optional[typing.Callable[[int], None]] = None,
        followlinks: bool = False) -> None:
    '''
    Copy file from source path to destination path

    Here are a few examples: ::

        >>> from tqdm import tqdm
        >>> from megfile import smart_copy, smart_stat
        >>> class Bar:
        ...     def __init__(self, total=10):
        ...         self._bar = tqdm(total=10)
        ...
        ...     def __call__(self, bytes_num):
        ...         self._bar.update(bytes_num)
        ...
        >>> src_path = 'test.png'
        >>> dst_path = 'test1.png'
        >>> smart_copy(src_path, dst_path, callback=Bar(total=smart_stat(src_path).size), followlinks=False)
        856960it [00:00, 260592384.24it/s]

    :param src_path: Given source path
    :param dst_path: Given destination path
    :param callback: Called periodically during copy, and the input parameter is the data size (in bytes) of copy since the last call
    :param followlinks: False if regard symlink as file, else True
    '''
    # this function contains plenty of manual polymorphism
    if smart_islink(src_path) and is_s3(dst_path) and not followlinks:
        return

    src_protocol = _extract_protocol(src_path)
    dst_protocol = _extract_protocol(dst_path)

    try:
        copy_func = _copy_funcs[src_protocol][dst_protocol]
    except KeyError:
        copy_func = _default_copy_func
    if copy_func == fs_copy:
        fs_copy(src_path, dst_path, callback=callback, followlinks=followlinks)
    else:
        copy_func(src_path, dst_path, callback=callback)  # pytype: disable=wrong-keyword-args


def smart_sync(
        src_path: PathLike,
        dst_path: PathLike,
        callback: typing.Optional[typing.Callable[[str, int], None]] = None,
        followlinks: bool = False) -> None:
    '''
    Sync file or directory on s3 and fs

    .. note ::

        When the parameter is file, this function bahaves like ``smart_copy``.

        If file and directory of same name and same level, sync consider it's file first.

    Here are a few examples: ::

        >>> from tqdm import tqdm
        >>> from threading import Lock
        >>> from megfile import smart_sync, smart_stat, smart_glob
        >>> class Bar:
        ...     def __init__(self, total_file):
        ...         self._total_file = total_file
        ...         self._bar = None
        ...         self._now = None
        ...         self._file_index = 0
        ...         self._lock = Lock()
        ...     def __call__(self, path, num_bytes):
        ...         with self._lock:
        ...             if path != self._now:
        ...                 self._file_index += 1
        ...                 print("copy file {}/{}:".format(self._file_index, self._total_file))
        ...                 if self._bar:
        ...                     self._bar.close()
        ...                 self._bar = tqdm(total=smart_stat(path).size)
        ...                 self._now = path
        ...            self._bar.update(num_bytes)
        >>> total_file = len(list(smart_glob('src_path')))
        >>> smart_sync('src_path', 'dst_path', callback=Bar(total_file=total_file))

    :param src_path: Given source path
    :param dst_path: Given destination path
    :param callback: Called periodically during copy, and the input parameter is the data size (in bytes) of copy since the last call
    '''
    src_path, dst_path = get_traditional_path(src_path), get_traditional_path(
        dst_path)
    for src_file_path in smart_scan(src_path, followlinks=followlinks):
        content_path = src_file_path[len(src_path):]
        if len(content_path):
            content_path = content_path.lstrip('/')
            dst_abs_file_path = smart_path_join(dst_path, content_path)
        else:
            # if content_path is empty, which means smart_isfile(src_path) is True, this function is equal to smart_copy
            dst_abs_file_path = dst_path
        copy_callback = partial(callback, src_file_path) if callback else None
        smart_copy(
            src_file_path,
            dst_abs_file_path,
            callback=copy_callback,
            followlinks=followlinks)


def smart_move(
        src_path: PathLike, dst_path: PathLike,
        followlinks: bool = False) -> None:
    '''
    Move file/directory on s3 or fs. `s3://` or `s3://bucket` is not allowed to move

    :param src_path: Given source path
    :param dst_path: Given destination path
    '''
    src_protocol = _extract_protocol(src_path)
    dst_protocol = _extract_protocol(dst_path)
    if src_protocol == dst_protocol:
        if src_protocol == 'fs':
            fs_rename(src_path, dst_path)
        elif src_protocol == 's3':
            s3_rename(src_path, dst_path)
        else:
            raise UnsupportedError(operation='smart_move', path=src_path)  # pragma: no cover
        return
    smart_sync(src_path, dst_path, followlinks=followlinks)
    smart_remove(src_path, followlinks=followlinks)


def _group_glob(globstr: str) -> typing.List[str]:
    '''
    Split path, and group them by protocol, return the glob list of same group.

    :param globstr: A glob string
    :returns: A glob list after being grouped by protocol
    '''
    glob_dict = defaultdict(list)
    expanded_glob = ungloblize(globstr)

    for single_glob in expanded_glob:
        protocol = _extract_protocol(single_glob)
        glob_dict[protocol].append(single_glob)

    group_glob_list = []

    for protocol, glob_list in glob_dict.items():
        group_glob_list.append(globlize(glob_list))
    return group_glob_list


def smart_glob(
        path: PathLike, recursive: bool = True,
        missing_ok: bool = True) -> typing.List[str]:
    '''
    Given path may contain shell wildcard characters, return path list in ascending alphabetical order, in which path matches glob pattern

    :param path: A path pattern may contain shell wildcard characters
    :param recursive: If False, this function will not glob recursively
    :param missing_ok: If False and target path doesn't match any file, raise FileNotFoundError
    '''
    result = []
    group_glob_list = _group_glob(str(path))
    for glob_path in group_glob_list:
        protocol = _extract_protocol(glob_path)
        if protocol == 'fs':
            glob_path_list = fs_glob(glob_path, recursive, missing_ok)
        elif protocol == 's3':
            glob_path_list = s3_glob(glob_path, recursive, missing_ok)
        else:
            raise UnsupportedError(operation='smart_glob', path=path)  # pragma: no cover
        result.extend(glob_path_list)
    return result


def smart_iglob(
        path: PathLike, recursive: bool = True,
        missing_ok: bool = True) -> typing.Iterator[str]:
    '''
    Given path may contain shell wildcard characters, return path iterator in ascending alphabetical order, in which path matches glob pattern

    :param path: A path pattern may contain shell wildcard characters
    :param recursive: If False, this function will not glob recursively
    :param missing_ok: If False and target path doesn't match any file, raise FileNotFoundError
    '''
    result = []
    group_glob_list = _group_glob(str(path))
    for glob_path in group_glob_list:
        protocol = _extract_protocol(glob_path)
        if protocol == 'fs':
            iglob_path = fs_iglob(glob_path, recursive, missing_ok)
        elif protocol == 's3':
            iglob_path = s3_iglob(glob_path, recursive, missing_ok)
        else:
            raise UnsupportedError(operation='smart_iglob', path=path)  # pragma: no cover
        result.append(iglob_path)
    iterableres = chain(*result)
    return iterableres


def smart_glob_stat(
        path: PathLike, recursive: bool = True,
        missing_ok: bool = True) -> typing.Iterator[FileEntry]:
    '''
    Given path may contain shell wildcard characters, return a list contains tuples of path and file stat in ascending alphabetical order, in which path matches glob pattern

    :param path: A path pattern may contain shell wildcard characters
    :param recursive: If False, this function will not glob recursively
    :param missing_ok: If False and target path doesn't match any file, raise FileNotFoundError
    '''
    result = []
    group_glob_list = _group_glob(str(path))
    for glob_path in group_glob_list:
        protocol = _extract_protocol(glob_path)
        if protocol == 'fs':
            stat = fs_glob_stat(glob_path, recursive, missing_ok)
        elif protocol == 's3':
            stat = s3_glob_stat(glob_path, recursive, missing_ok)
        else:
            raise UnsupportedError(operation='smart_glob_stat', path=path)  # pragma: no cover
        result.append(stat)
    iterableres = chain(*result)
    return iterableres


def smart_combine_open(
        path_glob: str, mode: str = 'rb',
        open_func=smart_open) -> CombineReader:
    '''Open a unified reader that supports multi file reading。

    :param path_glob: A path may contain shell wildcard characters
    :param mode: Mode to open file, supports 'rb'
    :returns: A ```CombineReader```
    '''
    file_objects = list(
        open_func(path, mode) for path in sorted(smart_glob(path_glob)))
    return combine(file_objects, path_glob)


def smart_save_content(path: PathLike, content: bytes) -> None:
    '''Save bytes content to specified path

    param path: Path to save content
    '''
    with smart_open(path, 'wb') as fd:
        fd.write(content)  # type: ignore


def smart_load_text(path: PathLike) -> str:
    '''
    Read content from path

    param path: Path to be read
    '''
    with smart_open(path) as fd:
        return fd.read()  # type: ignore


def smart_save_text(path: PathLike, text: str) -> None:
    '''Save text to specified path

    param path: Path to save text
    '''
    with smart_open(path, 'w') as fd:
        fd.write(text)  # type: ignore


def smart_cache(path, s3_cacher=S3Cacher, **options):
    '''Return a path to Posixpath Interface

    param path: Path to cache
    param s3_cacher: Cacher for s3 path
    param options: Optional arguments for s3_cacher
    '''
    if is_s3(path):
        return s3_cacher(path, **options)
    return NullCacher(path)


def smart_touch(path: PathLike):
    '''Create a new file on path

    param path: Path to create file
    '''
    with smart_open(path, 'w'):
        pass

def smart_load_content(
        path: PathLike, start: typing.Optional[int] = None,
        stop: typing.Optional[int] = None) -> bytes:
    '''
    Get specified file from [start, stop) in bytes

    :param path: Specified path
    :param start: start index
    :param stop: stop index
    :returns: bytes content in range [start, stop)
    '''
    if is_s3(path):
        return s3_load_content(path, start, stop)

    start, stop = get_content_offset(start, stop, fs_getsize(path))

    with open(path, 'rb') as fd:
        fd.seek(start)
        return fd.read(stop - start)


def smart_path_join(path: typing.Union[str, os.PathLike], *other_paths: typing.Union[str, os.PathLike]) -> str:
    protocol = _extract_protocol(path)
    if protocol == 'fs':
        return fspath(os.path.normpath(fs_path_join(path, *other_paths)))
    if protocol == 's3':
        return fspath(s3_path_join(path, *other_paths))
    raise UnsupportedError(operation='smart_path_join', path=path)  # pragma: no cover