"""Moect storage, with compatibility for an existing pre-rename installation."""
import os
import stat
import tempfile
import uuid
from pathlib import Path


def atomic_write_bytes(path, data):
    """Publish a cache entry only after the full value has been written."""
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def is_link(path):
    """Include Windows junctions and other reparse points in link checks."""
    try:
        return is_link_stat(Path(path).lstat())
    except OSError:
        return True


def is_link_stat(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0)
                                            & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))


def app_data_root():
    local=Path(os.environ.get('LOCALAPPDATA',tempfile.gettempdir()))
    current=local/'Moect'
    legacy=local/'WeChatExporter'
    if (current/'config.json').is_file():return current
    if (legacy/'config.json').is_file():return legacy
    return current
