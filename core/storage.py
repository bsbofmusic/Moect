"""Managed cache inventory, verified migration and narrowly scoped cleanup."""
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from pathlib import Path
from .paths import is_link, is_link_stat

MANAGED = ('archives', 'media-cache', 'avatar-cache', 'previews')
LIMITS_GB = (1, 3, 5, 10, 20, 0)


def valid_limit(value):
    return type(value) is int and value in LIMITS_GB


def checked_path(path):
    if not path.is_absolute() or is_link(path) or path.resolve() != path:
        raise ValueError('缓存目录包含外部链接，请先移除链接后重试。')


def file_entries(root, cancel, excluded=()):
    root = Path(root).absolute()
    if root in excluded or not os.path.lexists(root):
        return
    stack = [root]
    while stack:
        cancel()
        directory = stack.pop()
        checked_path(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                cancel()
                path = directory/entry.name
                if path in excluded:
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if is_link_stat(info):
                    raise ValueError('缓存目录包含外部链接，请先移除链接后重试。')
                if stat.S_ISDIR(info.st_mode):
                    stack.append(path)
                elif stat.S_ISREG(info.st_mode):
                    yield path, info


def files_below(root, cancel=lambda: None):
    for path, _ in file_entries(root, cancel):
        yield path


def managed_root(service, root):
    checked_path(root)
    if not root.is_dir():
        raise ValueError('缓存磁盘暂不可用。')
    if root == service.root.resolve():
        return root
    try:
        path = root/'.moect-storage.json'
        marker = json.loads(path.read_text(encoding='utf-8')) if not is_link(path) else None
        if isinstance(marker, dict) and marker.get('owner') == service.config['storage_id']:
            return root
    except (OSError, ValueError, KeyError):
        pass
    raise ValueError('该目录不是此软件管理的缓存目录。')


def protected_paths(service, root, *, snapshots=True):
    export_folders = [service.config['export_dir'], *(task['folder'] for task in service.config['tasks'] if task.get('folder'))]
    # A user may keep exports beside MoectData under the same parent.
    protected = [folder for value in export_folders if (folder := Path(value).resolve()).is_relative_to(root)]
    fields = ('root', 'assets_root', 'account_dir') if snapshots else ('account_dir',)
    for library in (service.library, service.config.get('library'), service._demo_previous):
        for field in fields:
            if library and library.get(field):
                protected.append(Path(library[field]).resolve())
    return protected


def overlaps(path, paths):
    return any(path.is_relative_to(other) or other.is_relative_to(path) for other in paths)


def file_stamp(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino


def file_digest(path, check):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            check()
            digest.update(block)
    return digest.digest()


def retired_roots(service):
    for path in dict.fromkeys(service.config['storage_history']):
        candidate = Path(path)
        try:
            if candidate.is_absolute() and candidate.is_dir() and candidate == candidate.resolve() and candidate != service.storage_root:
                yield managed_root(service, candidate)
        except (OSError, ValueError):
            continue


def restore_legacy_media_key(path, target, relative, check):
    if (len(relative.parts) != 3 or relative.parts[0] != 'media-cache'
            or not re.fullmatch('[0-9a-f]{64}', relative.parts[1])
            or relative.name not in ('emoticon-key.bin', 'image-keys.json')):
        return
    size = path.stat().st_size
    if not 0 < size <= 64 * 1024 or (relative.name == 'emoticon-key.bin' and size != 16):
        return
    check()
    if target.parent.resolve() != target.parent or os.path.lexists(target):
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        shutil.copyfile(path, temporary)
        if file_digest(path, check) != file_digest(temporary, check):
            return
        # Publish without overwriting any newer credentials.
        if os.name == 'nt':
            temporary.rename(target)
        else:
            os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def clean_migrated_copies(service, root, check, verified=None):
    """Retire old copies after verifying them against the active location."""
    protected = [service.storage_root, *protected_paths(service, root)]
    destination_protected = protected_paths(service, service.storage_root, snapshots=False)
    freed = 0
    for name in MANAGED:
        try:
            for path in files_below(root/name, check):
                if overlaps(path, protected):
                    continue
                relative = path.relative_to(root)
                target = service.storage_root/relative
                if overlaps(target, destination_protected):
                    continue
                try:
                    if path.resolve() != path or is_link(path):
                        continue
                    if verified is None and not os.path.lexists(target):
                        restore_legacy_media_key(path, target, relative, check)
                    if path.resolve() != path or is_link(path) or target.resolve() != target or is_link(target):
                        continue
                    before = file_stamp(path), file_stamp(target)
                    if verified is not None:
                        matches = verified.get(path) == before
                    else:
                        matches = before[0][0] == before[1][0] and file_digest(path, check) == file_digest(target, check)
                    if matches and before == (file_stamp(path), file_stamp(target)):
                        path.unlink()
                        freed += before[0][0]
                except OSError:
                    # A locked or disconnected source can be retried from storage settings.
                    continue
        except (OSError, ValueError):
            continue
    return freed


def prune_storage_history(service, roots, check):
    removed = set()
    for root in roots:
        check()
        try:
            managed_root(service, root)
        except (OSError, ValueError):
            continue
        protected = [service.storage_root, *protected_paths(service, root)]
        for name in MANAGED:
            category = root/name
            if not category.is_dir() or is_link(category):
                continue
            directories = []
            for directory, children, _ in os.walk(category, followlinks=False):
                check()
                path = Path(directory)
                children[:] = [name for name in children if not is_link(path/name)]
                directories.append(path)
            for path in reversed(directories):
                check()
                try:
                    if path.resolve() == path and not is_link(path) and not overlaps(path, protected):
                        path.rmdir()
                except OSError:
                    continue
        if root.is_dir() and not any(os.path.lexists(root/name) for name in MANAGED):
            removed.add(root)
    if not removed:
        return
    with service.lock:
        previous = service.config['storage_history']
        service.config['storage_history'] = [path for path in previous if Path(path) not in removed]
        try:
            service.save()
        except OSError:
            service.config['storage_history'] = previous
            return
    for root in removed - {service.root.resolve()}:
        marker = root/'.moect-storage.json'
        try:
            if managed_root(service, root) == root and list(root.iterdir()) == [marker]:
                marker.unlink()
                root.rmdir()
        except (OSError, ValueError):
            continue


def reconcile_storage_history(service, check, verified_root=None, verified=None):
    managed_root(service, service.storage_root)
    history = list(retired_roots(service))
    freed = 0
    for root in history:
        check()
        freed += clean_migrated_copies(service, root, check, verified if root == verified_root else None)
    prune_storage_history(service, history, check)
    return freed


def storage_info(service, cancel=lambda: None):
    result = []
    roots = (service.storage_root, *retired_roots(service))
    incomplete = any(Path(value) not in roots for value in service.config['storage_history'])
    for root in roots:
        cancel()
        try:
            managed_root(service, root)
        except (OSError, ValueError):
            incomplete = True
            continue
        protected = set(protected_paths(service, root, snapshots=False))
        if root != service.storage_root:
            protected.add(service.storage_root)
        categories = dict.fromkeys(MANAGED, 0)
        for name in MANAGED:
            category = root/name
            if any(category.is_relative_to(path) for path in protected):
                continue
            try:
                for _, info in file_entries(category, cancel, protected):
                    categories[name] += info.st_size
            except (OSError, ValueError):
                incomplete = True
        current = root == service.storage_root
        total = sum(categories.values())
        if current or total:
            result.append({'path': str(root), 'current': current, 'bytes': total, 'categories': categories})
    return {'path': str(service.storage_root), 'roots': result, 'warning': service.storage_warning, 'incomplete': incomplete,
            'bytes': sum(item['bytes'] for item in result)}


def migrate(service, parent, job):
    source = managed_root(service, service.storage_root)
    parent = Path(parent).resolve()
    if service.storage_warning:
        raise ValueError('请先连接原缓存磁盘，再迁移缓存。')
    check = lambda: service._check(job)
    if parent == source:
        freed = reconcile_storage_history(service, check)
        service._update(job, detail='已在使用所选缓存位置。', freed_bytes=freed)
        return
    if parent.is_relative_to(source):
        raise ValueError('所选文件夹位于当前缓存目录内，请选择上一级或其他位置。')
    for library in (service.library, service.config.get('library'), service._demo_previous):
        if library and library.get('account_dir') and parent.is_relative_to(Path(library['account_dir']).resolve()):
            raise ValueError('所选文件夹位于微信数据目录内，请选择其他位置。')
    for root in (service.root.resolve(), *retired_roots(service)):
        if any(parent.is_relative_to(root/name) for name in MANAGED):
            raise ValueError('所选文件夹位于旧缓存内部，请选择该缓存目录的上一级。')
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent/'MoectData'
    number = 2
    while destination.exists():
        destination = parent/('MoectData_' + str(number))
        number += 1
    staging = parent/('.moect-migration-' + uuid.uuid4().hex)
    protected = set(protected_paths(service, source, snapshots=False))
    inventory = [(path, info.st_size) for name in MANAGED for path, info in file_entries(source/name, check, protected)
                 if not overlaps(path, protected)]
    total = sum(size for _, size in inventory)
    if shutil.disk_usage(parent).free < total + 16 * 1024 * 1024:
        raise ValueError('目标磁盘可用空间不足，请选择其他位置。')
    staging.mkdir()
    committed = False
    published = False
    verified = {}
    try:
        written = 0
        for path, _ in inventory:
            check()
            checked_path(path)
            before = file_stamp(path)
            target = staging/path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with path.open('rb') as original, target.open('wb') as copied:
                while True:
                    check()
                    block = original.read(1024 * 1024)
                    if not block:
                        break
                    copied.write(block)
                    digest.update(block)
                    written += len(block)
            if digest.digest() != file_digest(target, check):
                raise ValueError('迁移文件校验失败，原缓存仍可使用。')
            if before != file_stamp(path):
                raise ValueError('迁移期间缓存发生变化，请稍后重试。')
            shutil.copystat(path, target)
            verified[path] = before, file_stamp(target)
            service._update(job, progress=min(95, int(95 * written/max(total, 1))), detail='正在复制并校验缓存…')
        (staging/'.moect-storage.json').write_text(json.dumps({'owner': service.config['storage_id']}), encoding='utf-8')
        check()
        os.rename(staging, destination)
        published = True
        with service.lock:
            check()
            old_config = dict(service.config)
            old_library = service.library
            old_demo = service._demo_previous
            def relocated(value):
                if not value or value.get('demo'):
                    return value
                value = dict(value)
                for field in ('root', 'assets_root'):
                    path = Path(value[field]).resolve() if value.get(field) else None
                    if path is not None and path.is_relative_to(source):
                        relative = path.relative_to(source)
                        if relative.parts and relative.parts[0] in MANAGED and not overlaps(path, protected):
                            value[field] = str(destination/relative)
                value['storage_root'] = str(destination)
                return value
            try:
                service.library = relocated(service.library)
                service._demo_previous = relocated(service._demo_previous)
                service.config['library'] = relocated(service.config.get('library'))
                service.config['storage_dir'] = str(destination)
                service.config['storage_history'] = list(dict.fromkeys([*service.config['storage_history'], str(source)]))
                service.save()
            except BaseException:
                service.config = old_config
                service.library, service._demo_previous = old_library, old_demo
                raise
            service.storage_root = destination
            service._storage_epoch += 1
            service._invalidate_connections()
            committed = True
        service._update(job, progress=95, detail='已切换到新位置，正在清理旧缓存…')
        freed = 0
        try:
            # The destination is committed. The old location stays registered if
            # Windows blocks any removals or the process exits during cleanup.
            freed = reconcile_storage_history(service, check, source, verified)
        except (OSError, ValueError):
            pass
        detail = ('缓存迁移完成。旧位置仍有保留文件，可在存储设置中查看或重试清理。'
                  if service.config['storage_history'] else '缓存已迁移，旧位置的缓存已清理。')
        service._update(job, detail=detail, freed_bytes=freed)
    finally:
        # Both targets are this operation's explicitly created children.
        for target in (staging, destination):
            if target.exists() and (target == staging or (published and not committed)):
                if target.resolve().parent != parent or is_link(target):
                    raise ValueError('迁移目录边界校验失败。')
                shutil.rmtree(target)


def cleanup(service, job, limit_bytes=None, total_bytes=None):
    check = lambda: service._check(job)
    if limit_bytes is not None and total_bytes is None:
        total_bytes = storage_info(service, check)['bytes']
    history = list(retired_roots(service))
    freed = 0
    skipped = 0
    if limit_bytes is None:
        for root in history:
            freed += clean_migrated_copies(service, root, check)
            service._update(job, freed_bytes=freed)

    def children(folder):
        nonlocal skipped
        check()
        if not os.path.lexists(folder):
            return []
        try:
            checked_path(folder)
            return list(folder.iterdir()) if folder.is_dir() else []
        except (OSError, ValueError):
            skipped += 1
            return []

    candidates = []
    for root in (service.storage_root, *history):
        check()
        try:
            managed_root(service, root)
        except (OSError, ValueError):
            skipped += 1
            continue
        root_protected = protected_paths(service, root)
        if root != service.storage_root:
            root_protected.append(service.storage_root)
        targets = []
        caches = [root/'previews', *(account/'downloads' for account in children(root/'media-cache'))]
        prune_avatars = limit_bytes is not None or root != service.storage_root
        if prune_avatars:
            caches.append(root/'avatar-cache')
        for account in children(root/'archives'):
            targets.extend(path for path in children(account) if re.fullmatch(r'(?:snapshot|assets)_[0-9a-f]{12}', path.name))
            caches.append(account/'moments-thumbnails')
            if prune_avatars:
                caches.append(account/'avatar-cache')
        for cache in caches:
            targets.extend(children(cache))

        for target in targets:
            check()
            if overlaps(target, root_protected):
                continue
            try:
                checked_path(target)
                info = target.stat()
                if stat.S_ISDIR(info.st_mode):
                    size = 0
                    modified = info.st_mtime_ns
                    for _, info in file_entries(target, check):
                        size += info.st_size
                        modified = max(modified, info.st_mtime_ns)
                elif stat.S_ISREG(info.st_mode):
                    size, modified = info.st_size, info.st_mtime_ns
                else:
                    continue
                candidates.append((modified, target, root, size))
            except (OSError, ValueError):
                skipped += 1
                continue
    for _, target, root, size in sorted(candidates):
        check()
        if limit_bytes is not None and total_bytes-freed <= limit_bytes:
            break
        try:
            managed_root(service, root)
            if not target.is_relative_to(root) or target==root:
                raise ValueError('清理目录边界校验失败。')
            checked_path(target)
            if target.is_dir():
                for _ in file_entries(target, check):
                    pass
                check()
                checked_path(target)
                shutil.rmtree(target)
            else:
                target.unlink()
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            skipped += 1
            continue
        freed += size
        service._update(job, detail='正在清理过期缓存…', freed_bytes=freed)
    prune_storage_history(service, history, check)
    detail = '清理完成，释放 %.1f MB。' % (freed/1024**2)
    if skipped:
        detail += f' 有 {skipped} 项暂无法清理。'
    if limit_bytes is not None and total_bytes-freed > limit_bytes:
        detail += ' 当前快照或保留文件仍超过上限，已保留，未强制删除。'
    service._update(job, detail=detail, freed_bytes=freed, skipped_entries=skipped)


def source_database_bytes(library):
    root = Path(library['account_dir'])/'db_storage' if library.get('account_dir') else Path(library.get('root') or '')
    if not library.get('root') or not root.is_dir():
        return 0
    try:
        return sum(path.stat().st_size for path in root.rglob('*') if path.is_file() and path.suffix in ('.db', '.db-wal', '.db-shm'))
    except OSError:
        return None
