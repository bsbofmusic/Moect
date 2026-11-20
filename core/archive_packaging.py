"""Remember export choices with Windows DPAPI and create verified ZIP archives."""
import base64
import ctypes
import os
import zipfile
from pathlib import Path
from ctypes import wintypes
from time import localtime


def protect_password(value, decrypt=False):
    if os.name != 'nt':
        raise ValueError('密码记忆需要 Windows 当前用户加密服务。')
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy_value = b'Moect archive password v1'
    entropy_buffer = ctypes.create_string_buffer(entropy_value)
    entropy = Blob(len(entropy_value), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    if not function(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(result)):
        raise ValueError('已保存的密码无法读取，请重新设置导出密码。' if decrypt else '密码保存失败，请检查 Windows 用户加密服务。')
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


def public_options(value):
    value = value if isinstance(value, dict) else {}
    encrypted = value.get('encrypt') is True
    return {'zip': value.get('zip') is True or encrypted, 'encrypt': encrypted,
            'has_password': isinstance(value.get('protected_password'), str) and bool(value.get('protected_password'))}


def update_options(current, payload):
    current = current if isinstance(current, dict) else {}
    result = {'zip': current.get('zip') is True, 'encrypt': current.get('encrypt') is True}
    if isinstance(current.get('protected_password'), str):
        result['protected_password'] = current['protected_password']
    for field in ('zip', 'encrypt'):
        if field in payload:
            if not isinstance(payload[field], bool):
                raise ValueError('压缩与加密选项无效。')
            result[field] = payload[field]
    if payload.get('zip') is False and 'encrypt' not in payload:
        result['encrypt'] = False
    if result.get('encrypt'):
        result['zip'] = True
    if 'password' in payload:
        password = payload['password']
        if not isinstance(password, str) or len(password) > 256:
            raise ValueError('密码应为不超过 256 个字符的文字。')
        if password:
            result['protected_password'] = base64.b64encode(protect_password(password.encode('utf-8'))).decode('ascii')
    return result


def export_options(current, payload):
    updates = {}
    for target, source in (('zip', 'archiveZip'), ('encrypt', 'archiveEncrypt'), ('password', 'archivePassword')):
        if source in payload:
            updates[target] = payload[source]
    options = update_options(current, updates)
    password = None
    if options.get('encrypt'):
        value = options.get('protected_password')
        if not value:
            raise ValueError('请先设置加密压缩包密码。')
        try:
            password = protect_password(base64.b64decode(value, validate=True), decrypt=True)
        except (ValueError, TypeError) as error:
            raise ValueError('已保存的密码无法读取，请重新设置导出密码。') from error
    return options, password


def archive_files(folder, cancel):
    folder = Path(folder).resolve()
    for path in folder.rglob('*'):
        cancel()
        if path.is_symlink() or not path.resolve().is_relative_to(folder):
            raise ValueError('归档目录包含无效的链接文件。')
        if path.is_file():
            yield path, path.stat().st_size


def package_folder(folder, destination, password, cancel, status, progress=lambda _: None, *, root_name=None, files=None):
    folder, destination = Path(folder).resolve(), Path(destination)
    if files is None:
        files = list(archive_files(folder, cancel))
    total = max(1, sum(size for _, size in files))
    processed = 0
    last_percent = -1
    def advance(size):
        nonlocal processed, last_percent
        processed += size
        percent = min(100, processed * 50 // total)
        if percent != last_percent:
            last_percent = percent
            progress(percent / 100)
    if password:
        import pyzipper
        archive = pyzipper.AESZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED,
                                     compresslevel=6, encryption=pyzipper.WZ_AES, allowZip64=True)
        archive.setpassword(password)
        archive.setencryption(pyzipper.WZ_AES, nbits=256)
        entry_type = archive.zipinfo_cls
    else:
        archive = zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True)
        entry_type = zipfile.ZipInfo
    already_compressed = {'.jpg','.jpeg','.png','.gif','.webp','.mp4','.mov','.webm','.mkv','.mp3','.m4a','.flac'}
    try:
        with archive:
            for number, (path, size) in enumerate(sorted(files), 1):
                cancel()
                status('正在压缩 %d / %d 个文件…' % (number, len(files)))
                member = path.relative_to(folder).as_posix()
                if root_name:
                    member = root_name + '/' + member
                if path.suffix.lower() in already_compressed:
                    entry = entry_type(member, date_time=localtime()[:6])
                    entry.compress_type = zipfile.ZIP_STORED
                    entry.file_size = size
                    member = entry
                with path.open('rb') as source, archive.open(member, 'w', force_zip64=True) as target:
                    while True:
                        cancel()
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        target.write(block)
                        advance(len(block))
        status('正在校验压缩包完整性…')
        reader = pyzipper.AESZipFile if password else zipfile.ZipFile
        with reader(destination) as verified:
            if password:
                verified.setpassword(password)
            for entry in verified.infolist():
                with verified.open(entry) as source:
                    while True:
                        cancel()
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        advance(len(block))
        cancel()
        progress(1)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def folder_sizes(folder, cancel=lambda: None, *, files=None):
    result = {'bytes': 0, 'resources': {}}
    folder = Path(folder).resolve()
    for path, size in (archive_files(folder, cancel) if files is None else files):
        cancel()
        result['bytes'] += size
        parts = path.relative_to(folder).parts
        if 'resources' in parts:
            index = parts.index('resources')
            category = parts[index + 1] if len(parts) > index + 2 else 'other'
            result['resources'][category] = result['resources'].get(category, 0) + size
    return result
