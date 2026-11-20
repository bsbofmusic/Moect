"""Contact avatar lookup shared by the UI and portable exports."""
import base64
import hashlib
import sqlite3
import urllib.request
from collections import OrderedDict
from contextlib import closing
from functools import cached_property
from pathlib import Path
from urllib.parse import urlparse,quote
from wx4.reader import sqlite_text
from .paths import atomic_write_bytes


def image_type(blob):
    for signature, ext, mime in ((b'\xff\xd8\xff','jpg','image/jpeg'),
            (b'\x89PNG\r\n\x1a\n','png','image/png'),(b'GIF8','gif','image/gif'),
            (b'RIFF','webp','image/webp'),(b'BM','bmp','image/bmp')):
        if blob.startswith(signature):
            if ext=='webp' and blob[8:12]!=b'WEBP':continue
            return ext,mime
    return None


def avatar_host(url):
    if not isinstance(url,str):return False
    try:
        p=urlparse(url);host=(p.hostname or '').lower()
        return p.scheme in ('http','https') and not p.username and p.port in (None,80,443) and any(
            host==d or host.endswith('.'+d) for d in ('qpic.cn','qlogo.cn','weixin.qq.com'))
    except ValueError:
        return False


def local_avatar(con, tables, username):
    def identifier(name):
        return '"' + name.replace('"', '""') + '"'

    for table in tables:
        columns = {row[1].lower(): row[1] for row in con.execute('PRAGMA table_info('+identifier(table)+')')}
        user = next((columns[c] for c in ('username','user_name','usrname','wxid') if c in columns), None)
        if not user:
            continue
        row = con.execute('SELECT * FROM '+identifier(table)+' WHERE '+identifier(user)+'=? LIMIT 1',
                          (username,)).fetchone()
        if row:
            for value in row:
                if isinstance(value, bytes) and len(value) <= 2*1024**2 and image_type(value):
                    return value
    return None


def snapshot_avatar(sources, username):
    for path, tables in sources:
        try:
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as con:
                con.text_factory = sqlite_text
                blob = local_avatar(con, tables, username)
                if blob:
                    return blob
        except sqlite3.Error:
            continue
    return None


class AvatarStore:
    def __init__(self, data, cache=None, legacy_cache=None):
        self.data=data
        self.cache=Path(cache) if cache else None
        self.legacy_cache=Path(legacy_cache) if legacy_cache else None
        self.lookups = OrderedDict()
        self.lookup_bytes = 0

    @classmethod
    def for_archive(cls, data):
        legacy = Path(data.root).parent/'avatar-cache'
        root = getattr(data, 'cache_root', None)
        return cls(data, Path(root)/'avatar-cache', legacy) if root else cls(data, legacy)

    def snapshot_sources(self):
        return tuple((Path(self.data.root, rel).resolve(), tuple(self.data.tables_of(con)))
                     for rel, con in self.data._conns.items()
                     if con and any(part in rel.lower() for part in ('head','avatar')))

    def blob(self, username, download=False):
        key=(username,download)
        if key in self.lookups:
            self.lookups.move_to_end(key)
            return self.lookups[key]
        found=None
        # head_image.db contains local avatar blobs on installations where
        # network avatar URLs are absent or expired.
        for rel,con in self.data._conns.items():
            if not con or not any(t in rel.lower() for t in ('head','avatar')):continue
            found=local_avatar(con, self.data.tables_of(con), username)
            if found:break
        if not found:
            found=self.url_blob(self.data.avatar_of(username), download)
        self.lookups[key] = found
        self.lookup_bytes += len(found or b'')
        while len(self.lookups) > 2048 or self.lookup_bytes > 32 * 1024**2:
            _, old_blob = self.lookups.popitem(last=False)
            self.lookup_bytes -= len(old_blob or b'')
        return found

    def url_blob(self, url, download=False):
        if not url:
            return None
        name = hashlib.sha256(url.encode()).hexdigest()+'.img'
        for root in dict.fromkeys((self.cache, self.legacy_cache)):
            if root is None:
                continue
            try:
                with (root/name).open('rb') as stream:
                    blob=stream.read(2*1024**2+1)
                if len(blob)<=2*1024**2 and image_type(blob):
                    return blob
            except OSError:
                pass
        if download and avatar_host(url):
            try:
                class Redirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, req, fp, code, msg, headers, newurl):
                        if not avatar_host(newurl):raise ValueError('Unsupported avatar redirect')
                        return super().redirect_request(req,fp,code,msg,headers,newurl)
                with urllib.request.build_opener(Redirect).open(url,timeout=3) as response:
                    blob=response.read(2*1024*1024+1)
                if len(blob)<=2*1024*1024 and image_type(blob):
                    if self.cache:
                        try:
                            self.cache.mkdir(parents=True,exist_ok=True)
                            atomic_write_bytes(self.cache/name, blob)
                        except OSError:
                            pass
                    return blob
            except (OSError,ValueError):pass
        return None

    def ui(self, username):
        blob=self.blob(username)
        if blob:return 'data:'+image_type(blob)[1]+';base64,'+base64.b64encode(blob).decode()
        url=self.data.avatar_of(username)
        return url if avatar_host(url) else ''

    @cached_property
    def reference_prefix(self):
        if any('head' in rel.lower() or 'avatar' in rel.lower() for rel in self.data._conns):
            identity=hashlib.sha256(str(Path(self.data.root).resolve()).encode()).hexdigest()[:16]
            return 'moect-avatar:'+identity+':'
        return ''

    def reference(self, username):
        """Keep session lists small; local avatar bytes are fetched on demand."""
        if self.reference_prefix:
            return self.reference_prefix+quote(username,safe='')
        url=self.data.avatar_of(username)
        return url if avatar_host(url) else ''

    def export(self, username, folder, *, download=True):
        blob=self.blob(username,download)
        if not blob:return ''
        ext,_=image_type(blob)
        path=Path(folder)/'avatars'/(hashlib.sha256(username.encode()).hexdigest()[:24]+'.'+ext)
        path.parent.mkdir(parents=True,exist_ok=True)
        if not path.exists():path.write_bytes(blob)
        return path.relative_to(folder).as_posix()
