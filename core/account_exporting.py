"""Portable sticker albums and explicitly scoped cached Moments exports."""
import hashlib
import json
import os
import base64
import tempfile
import threading
import time
from collections import OrderedDict, deque
from contextlib import closing
from datetime import datetime
from pathlib import Path
from itertools import chain, islice

from .account_assets import StickerCatalog,MomentsCatalog,HASH,moment_selection,moment_preview
from .avatars import image_type
from .exporting import safe_name,clean
from .resources import ResourceStore
from .sticker_cache import StickerCache
from .media_codec import encrypted_media_variants,media_type
from .version import APP_NAME,VERSION
from .sns_codec import sns_variants
from .themes import archive_viewer_template,archive_theme
from .inline_faces import ArchiveFaces
from .paths import atomic_write_bytes
from .streaming import export_capacity, export_worker_limit, json_line, run_export_workers, write_archive_data
from wx4 import WECHAT_TIME_ZONE


def write_collection(folder,assets,data,cancel=lambda:None):
    faces=ArchiveFaces(assets,cancel)
    if data.get('category')=='moments':
        for item in data.get('items',[]):
            faces.remember_text(item.get('text'))
            for comment in item.get('comments',[]):
                faces.remember_text(comment.get('text'))
    faces.write(folder)
    data['theme']=archive_theme(data.get('theme'))
    template=archive_viewer_template(assets,'collection.html',data['theme'])
    write_archive_data(folder,template,'__COLLECTION__',data,cancel)


def collection_media(records, store, process, fork, prepare, needs_media, status):
    """Resolve bounded groups in isolation and publish files in input order."""
    records = iter(records)
    limit = export_worker_limit()
    capacity = None
    while window := list(islice(records, 16)):
        selected = [(i, item) for i, item in enumerate(window) if needs_media(item)]
        if limit == 1 or len(selected) < 2:
            for item in window:
                store.cancel()
                yield process(item)
            continue
        prepare([item for _, item in selected])
        if capacity is None:
            capacity = export_capacity()
        group_size = (len(selected) + limit - 1) // limit
        groups = [selected[i:i+group_size] for i in range(0, len(selected), group_size)]
        with tempfile.TemporaryDirectory(prefix='.media-', dir=store.out) as temporary:
            def work(group):
                folder = Path(temporary)/str(group[0][0])
                folder.mkdir()
                with closing(ResourceStore(store.data, '', folder, store.cancel, status,
                                           {'includeAvatars': False}, report=False)) as worker:
                    resolve = fork(worker)
                    results = {i: resolve(item) for i, item in group}
                    return folder, results, worker.stats

            workers = run_export_workers(groups, work, store.cancel,
                                         lambda: status('系统负载较高，等待可用资源…'),
                                         ordered=True, limit=limit, capacity=capacity)
            with closing(workers):
                results = {}
                selected_indices = {i for i, _ in selected}
                for i, item in enumerate(window):
                    store.cancel()
                    if i not in selected_indices:
                        yield process(item)
                        continue
                    if i not in results:
                        folder, results, stats = next(workers)
                        for kind in (folder/'resources').glob('*'):
                            dest = store.out/'resources'/kind.name
                            dest.mkdir(parents=True, exist_ok=True)
                            for file in kind.iterdir():
                                store.cancel()
                                target = dest/file.name
                                if not target.exists():
                                    file.replace(target)
                        for key, count in stats.items():
                            store.stats[key] += count
                    yield results.pop(i)


def metadata(name,category,profile):
    return {'application':APP_NAME,'version':VERSION,'category':category,'name':name,'owner':{'name':profile['name'],'uid':profile['wxid']},
            'exported_at':datetime.now(WECHAT_TIME_ZONE).replace(tzinfo=None).isoformat(timespec='seconds'),
            'time_zone':WECHAT_TIME_ZONE.tzname(None),'partial_local_cache':True}


def export_stickers(archive,library,payload,folder,assets,progress,cancel,status):
    catalog=StickerCatalog(library,cancel)
    wanted=payload.get('groups')
    if not isinstance(wanted,list) or not all(isinstance(v,str) for v in wanted):raise ValueError('请选择需要导出的表情分类。')
    by_id={g['id']:g for g in catalog.groups}
    if not wanted or any(key not in by_id for key in wanted):raise ValueError('表情分类已变化，请刷新后重新选择。')
    selected=[g for g in catalog.groups if g['id'] in set(wanted)]
    download=payload.get('download',True)
    if not isinstance(download,bool):raise ValueError('下载选项无效')
    total=sum(len(g['members']) for g in selected)
    if not total:raise ValueError('所选分类中没有可导出的表情。')
    profile=archive.profile();name=profile['name']+' · 表情包'
    data=metadata(name,'stickers',profile);data.update(groups=[],items=[],notice=catalog.summary()['notice'],theme=archive_theme(payload.get('_theme')))
    resources=ResourceStore(archive.data,'',folder,cancel,status,{'includeAvatars':False})
    decoder=StickerCache(archive.data,resources.codec,cancel,status);count=0
    try:
        for group_number,group in enumerate(selected,1):
            cancel();relative=Path('表情包')/(str(group_number).zfill(2)+'_'+safe_name(group['name'])[:40])
            destination=folder/relative;destination.mkdir(parents=True,exist_ok=True)
            data['groups'].append({'id':group['id'],'name':group['name'],'count':len(group['members'])})
            for number,member in enumerate(group['members'],1):
                cancel();count+=1;progress(count,total)
                image,quality=decoder.resolve(member,resources,download)
                item={'id':group['id']+'_'+member['md5'],'group':group['id'],'name':member.get('caption') or '表情 '+str(number),
                      'md5':member['md5'],'status':'available' if image else 'missing','quality':quality,'resources':[]}
                if image:
                    extension=image_type(image)[0]
                    filename=str(number).zfill(3)+'_'+safe_name(member.get('caption') or member['md5'][:12])[:50]+'.'+extension
                    path=relative/filename;(folder/path).write_bytes(image)
                    item['resources']=[{'kind':'image','path':path.as_posix(),'status':'available','role':'thumbnail' if quality=='thumbnail' else 'main'}]
                    resources.stats['available']+=1
                else:resources.stats['missing']+=1
                resources.report.write(json_line({k:item[k] for k in ('id','group','name','md5','status','quality')}))
                data['items'].append(item)
        data['resource_summary']=dict(resources.stats)
    finally:resources.close()
    cancel();write_collection(folder,assets,data,cancel)
    return {'name':name,'path':str(folder/'index.html'),'count':count,'format':'stickers','resource_summary':data['resource_summary']}


class MomentResources:
    def __init__(self,resources,cache=None,index_seconds=None,max_image_bytes=64*1024*1024):
        self.resources=resources;self.index=None;self.cache=Path(cache).resolve() if cache else None;self.cached={}
        self.index_seconds=index_seconds;self.max_image_bytes=max_image_bytes
        self.key_samples=None;self.last_failure=None
        self.source=None
        self.dat_lock=threading.Lock()
        self.index_lock=threading.Lock()
        self.converting=threading.Condition()
        self.in_flight=set()
        self.converted=OrderedDict()
        if self.cache and (self.cache/'media.json').is_file():
            try:
                cache_data=json.loads((self.cache/'media.json').read_text(encoding='utf-8'))
                if isinstance(cache_data,dict):self.cached=cache_data
            except (OSError,ValueError):pass

    @staticmethod
    def media_key(item):return hashlib.sha256(json.dumps(item,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

    def fork(self, resources):
        resolver = MomentResources(resources, max_image_bytes=self.max_image_bytes)
        resolver.cache, resolver.cached = self.cache, self.cached
        resolver.source = self
        return resolver

    def decode_dat(self, raw, path, hints):
        # Keep key recovery, sample selection and the scan budget shared by the export.
        with self.dat_lock:
            store = self.resources
            store.cancel()
            if self.key_samples is None:
                from .image_recovery import recent_image_samples
                self.key_samples = recent_image_samples(store.data,store.cancel) if store.keys.scan_remaining>0 else []
            decoded = store.keys.decode(raw,path,self.key_samples,hints.get('keys',[]))
            image = store.keys.rendered or (store.codec.image(decoded) if decoded else None)
            reason = store.keys.reason or 'image_decode_failed'
            return image, {'status':'encrypted' if reason in ('aes_key_unavailable','xor_unresolved') else 'corrupt',
                           **store.keys.diagnostics,'reason':reason}

    def _local_index(self):
        with self.index_lock:
            if self.index is not None:return self.index
            self.resources.keys.progress('正在查找本机朋友圈附件…')
            index={};account=Path(self.resources.data.account_dir()).resolve();cache=account/'cache'
            deadline=time.monotonic()+self.index_seconds if self.index_seconds is not None else float('inf')
            if cache.is_dir():
                for month in sorted(cache.iterdir(),reverse=True):
                    if time.monotonic()>=deadline:break
                    if not month.is_dir() or month.is_symlink():continue
                    base=month/'Sns'
                    if not base.is_dir() or base.is_symlink():continue
                    for directory,dirs,files in os.walk(base,followlinks=False):
                        if time.monotonic()>=deadline:break
                        self.resources.cancel();dirs[:]=[n for n in dirs if not (Path(directory)/n).is_symlink()]
                        for name in files:
                            if time.monotonic()>=deadline:break
                            p=Path(directory)/name
                            stem=p.stem.lower()
                            digest=stem if HASH.fullmatch(stem) else (p.parent.name+stem).lower()
                            if HASH.fullmatch(digest) and not p.is_symlink() and p.resolve().is_relative_to(account):
                                index.setdefault(digest,[]).append(p)
            self.index=index
            return index

    def local_candidates(self,hints):
        index=(self.source or self)._local_index()
        result=[]
        for digest in hints.get('hashes',[]):
            for path in index.get(digest,[]):
                thumbnail=digest in hints.get('thumbnail_hashes',[]) and digest not in hints.get('main_hashes',[]) or path.suffix.lower()=='.thumb' or any('thumb' in part.lower() for part in path.parts)
                result.append((path,'thumbnail' if thumbnail else 'main'))
        result.sort(key=lambda row:(row[1]!='main',row[0].suffix.lower()=='.tmp'))
        return result

    def convert(self,path,kind,hints):
        store=self.resources
        self.last_failure={'status':'corrupt','reason':'image_decode_failed'}
        try:
            stat = path.stat()
            if stat.st_size>(256*1024*1024 if kind=='video' else self.max_image_bytes):
                self.last_failure={'status':'unsupported','reason':'file_too_large'};return None
            identity=(path,kind,bool(hints.get('animated')),stat.st_size,stat.st_mtime_ns,
                      tuple(hints.get('stream_keys',())),tuple(hints.get('keys',())))
            shared=self.source or self
            with shared.converting:
                while identity in shared.in_flight:
                    store.cancel()
                    shared.converting.wait(.2)
                if identity in shared.converted:
                    shared.converted.move_to_end(identity)
                    store.stats['available']+=1
                    return dict(shared.converted[identity])
                shared.in_flight.add(identity)
            result=None
            try:
                result=self._convert(path,kind,hints)
                return result
            finally:
                with shared.converting:
                    # The owner's group will publish the file even if another group references it first.
                    if result:
                        shared.converted[identity]=dict(result)
                        if len(shared.converted)>1024:shared.converted.popitem(last=False)
                    shared.in_flight.remove(identity)
                    shared.converting.notify_all()
        except OSError:self.last_failure={'status':'missing','reason':'local_file_unreadable'}
        except ValueError:pass
        return None

    def _convert(self,path,kind,hints):
        store=self.resources
        if kind=='video':
            with path.open('rb') as stream:signature=media_type(stream.read(32))
            original=path
            if not signature or not signature[1].startswith('video/'):
                # SNS video encryption affects only the first 128 KiB.
                with path.open('rb') as stream:prefix=stream.read(128*1024)
                decoded=next((candidate for candidate in sns_variants(prefix,hints.get('stream_keys',[]),store.cancel)
                              if media_type(candidate) and media_type(candidate)[1].startswith('video/')),None)
                if decoded is None:return None
                target=Path(store.tmp.name)/'sns-decrypted.mp4'
                with path.open('rb') as source,target.open('wb') as output:
                    source.seek(len(prefix));output.write(decoded)
                    while True:
                        store.cancel();block=source.read(1024*1024)
                        if not block:break
                        output.write(block)
                path=target
            result=store.codec.video(path,Path(store.tmp.name)/'sns-playback.mp4')
            if result:return store._copy(result,'video',extension='.mp4',identity_source=original)
        else:
            raw=path.read_bytes()
            if raw.startswith((b'\x07\x08V1',b'\x07\x08V2')):
                image,failure=(self.source or self).decode_dat(raw,path,hints)
                if image:return store._copy(path,'image',image,'.'+image_type(image)[0])
                self.last_failure=failure
                return None
            for blob in chain((raw,),sns_variants(raw,hints.get('stream_keys',[]),store.cancel),encrypted_media_variants(raw,hints.get('keys',[]),store.cancel)):
                image=(store.codec.sticker if hints.get('animated') else store.codec.image)(blob)
                if image:return store._copy(path,'image',image,'.'+image_type(image)[0])
        return None

    def resolve(self,item,include_images,include_videos):
        store=self.resources;kind=item['kind'];hints=item['_media']
        if (kind=='image' and not include_images) or (kind=='video' and not include_videos):
            store.stats['skipped']+=1;return {'kind':kind,'status':'skipped','resources':[]}
        cached=self.cached.get(self.media_key(item)) if self.cached else None
        if cached and self.cache:
            copied=[]
            for resource in cached.get('resources',[]):
                try:
                    source=(self.cache/resource['path']).resolve()
                    if not source.is_relative_to(self.cache) or not source.is_file():continue
                    if resource['kind']=='image' and source.stat().st_size>self.max_image_bytes:continue
                    result=store._copy(source,resource['kind'],extension=source.suffix)
                except OSError:
                    continue
                if result:
                    if resource.get('role'):result['role']=resource['role']
                    copied.append(result)
            if copied:return {**cached,'resources':copied}
        thumbnails=[];failures=[]
        for source,role in self.local_candidates(hints):
            if role=='thumbnail':thumbnails.append(source);continue
            resource=self.convert(source,kind,hints)
            if resource:return {'kind':kind,'status':'available','resources':[resource]}
            if self.last_failure:failures.append(dict(self.last_failure))
        if include_images:
            for source in thumbnails:
                resource=self.convert(source,'image',hints)
                if resource:
                    resource['role']='thumbnail';return {'kind':kind,'status':'thumbnail','resources':[resource]}
                if self.last_failure:failures.append(dict(self.last_failure))
        if failures:
            failure=next((value for value in failures if value['status']=='encrypted'),failures[0])
            store.stats[failure['status']]+=1
            return {'kind':kind,'status':failure['status'],'reason':failure['reason'],
                    'diagnostics':failure,'resources':[]}
        store.stats['missing']+=1;return {'kind':kind,'status':'missing','resources':[]}


def local_moments_previews(archive,library,posts):
    """Read local media only. No WeChat UI, process scan or network request."""
    if not posts:return []
    import io
    import tempfile
    from PIL import Image
    previews=[];legacy_cache=Path(library['root']).parent/'moments-thumbnails';deadline=time.monotonic()+6
    cache_root=getattr(archive.data,'cache_root',None)
    cache=Path(cache_root)/'previews' if cache_root else legacy_cache
    scope=str(Path(library.get('account_dir') or library['root']).resolve())
    with tempfile.TemporaryDirectory(prefix='moect-moments-preview-') as temporary:
        resources=ResourceStore(archive.data,'',Path(temporary),options={'includeAvatars':False})
        resources.keys.scan_remaining=0 # Previews reuse saved credentials; no process scans.
        resolver=MomentResources(resources,Path(library.get('assets_root') or library['root'])/'preview',index_seconds=2,max_image_bytes=8*1024*1024)
        try:
            for post in posts:
                item=moment_preview(post,archive.data.name_of);item['thumbnails']=[];item['media_status']=[]
                item['image_count']=sum(m['kind']=='image' for m in post['media'][:9])
                for media in post['media'][:9]:
                    if media['kind']!='image':continue
                    marker=hashlib.sha256((str(library.get('assets_revision',''))+MomentResources.media_key(media)).encode()).hexdigest()
                    legacy=legacy_cache/(marker+'.jpg')
                    token=hashlib.sha256((scope+'\0'+marker).encode()).hexdigest()
                    target=cache/('moments-'+token+'.jpg') if cache_root else legacy
                    blob=None
                    for source in dict.fromkeys((target,legacy)):
                        try:
                            with source.open('rb') as stream:
                                value=stream.read(128*1024+1)
                            if 0<len(value)<=128*1024:
                                blob=value
                                break
                        except OSError:
                            continue
                    try:
                        if blob is None and time.monotonic()<deadline:
                            result=resolver.resolve(media,True,False)
                            item['media_status'].append(result['status'])
                            for resource in result['resources']:
                                if resource['kind']!='image':continue
                                with Image.open(Path(temporary)/resource['path']) as image:
                                    image.seek(0);image.thumbnail((220,220));output=io.BytesIO();image.convert('RGB').save(output,format='JPEG',quality=72)
                                blob=output.getvalue();cache.mkdir(parents=True,exist_ok=True);atomic_write_bytes(target,blob);break
                    except (OSError,ValueError):pass
                    if blob:item['thumbnails'].append('data:image/jpeg;base64,'+base64.b64encode(blob).decode())
                previews.append(item)
        finally:resources.close()
    return previews


def export_moments(archive,library,payload,folder,assets,progress,cancel,status):
    images=payload.get('includeImages',True);videos=payload.get('includeVideos',True)
    if not isinstance(images,bool) or not isinstance(videos,bool):raise ValueError('资源导出选项无效')
    profile=archive.profile();selection=moment_selection(payload,profile['wxid'],archive.data.contacts)
    catalog=MomentsCatalog(library,profile['wxid'],selection['scope'],selection['author_id']);name=profile['name']+' · '+selection['label']
    data=metadata(name,'moments',profile);data.update(items=[],scope=selection,theme=archive_theme(payload.get('_theme')),filters={k:payload[k] for k in ('start','end') if payload.get(k)},
        notice='范围：'+selection['label']+'。包含本次已整理的可访问动态、评论和点赞；微信权限和资源有效期可能使结果不完整。HTML 内可按作者和时间继续筛选本次归档。')
    status('正在筛选待导出的朋友圈…')
    with closing(catalog.posts(cancel,start=payload.get('_start'),end=payload.get('_end'))) as source:
        posts=deque(source)
    if not catalog.available:raise ValueError('尚无可读取的朋友圈数据库。请先在微信浏览朋友圈，再刷新资源快照。')
    if not posts:raise ValueError('本机尚未缓存所选范围和时间段的朋友圈。请在微信浏览相应时间线或账号主页，再刷新资源快照。')
    total=len(posts)
    progress(0,total)
    resources=ResourceStore(archive.data,'',folder,cancel,status,{'includeAvatars':False})
    resolver=MomentResources(resources,Path(library.get('assets_root') or library['root'])/'preview')
    avatars={}
    def avatar(uid):
        if uid and uid not in avatars:
            avatars[uid]=archive.avatars.export(uid,folder) or ''
        return avatars.get(uid,'')

    def needs_media(post):
        return any(images if media['kind']=='image' else videos for media in post['media']) or (
            images and any(comment.get('_attachments') for comment in post.get('comments',[])))

    def with_media(worker):
        def resolve(post):
            comments=[]
            for comment in post.get('comments',[]):
                attachments=[]
                for media in comment.get('_attachments',[]):
                    cancel()
                    attachments.extend(worker.resolve(media,images,False)['resources'])
                comments.append(attachments)
            media=[]
            for attachment in post['media']:
                cancel()
                media.append(worker.resolve(attachment,images,videos))
            return post,comments,media
        return resolve

    def prepare(_):
        status('正在处理朋友圈附件…')

    entries=collection_media((posts.popleft() for _ in range(total)),resources,with_media(resolver),
                             lambda worker: with_media(resolver.fork(worker)),prepare,needs_media,status)
    with closing(resources), closing(entries):
        for count,(post,comment_media,post_media) in enumerate(entries,1):
            item=clean({key:value for key,value in post.items() if key!='media'})
            item.update(resources=[],media_status=[],media_diagnostics=[])
            item['author_name']=archive.data.name_of(post['author']) or post['author']
            item['author_avatar']=avatar(post['author'])
            for person in item.get('likes',[])+item.get('comments',[]):
                cancel();uid=person['uid'];contact=archive.data.contacts.get(uid) or {}
                person['name']=contact.get('remark') or contact.get('nick_name') or person['name'] or uid
                person['avatar']=avatar(uid)
                if person.get('reply_uid'):
                    person['reply_name']=archive.data.name_of(person['reply_uid']) or person.get('reply_name') or person['reply_uid']
            for comment,attachments in zip(item.get('comments',[]),comment_media):
                comment['resources']=attachments
            if post['status']=='unsupported':resources.stats['unsupported']+=1
            for result in post_media:
                item['resources'].extend(result['resources']);item['media_status'].append(result['status'])
                item['media_diagnostics'].append({k:result[k] for k in ('kind','status','reason','diagnostics') if k in result})
            if item['create_time']:
                try:item['time_str']=datetime.fromtimestamp(item['create_time'],WECHAT_TIME_ZONE).strftime('%Y-%m-%d %H:%M:%S')
                except (ValueError,OverflowError,OSError):item['time_str']='时间未知'
            resources.report.write(json_line({'id':item['id'],'status':item['status'],'media_status':item['media_status'],
                                              'media_diagnostics':item['media_diagnostics']}))
            data['items'].append(item)
            progress(count,total)
        data['resource_summary']=dict(resources.stats)
    data['items'].sort(key=lambda p:(p['create_time'],p['id']),reverse=True)
    status('正在写入朋友圈归档…')
    cancel();write_collection(folder,assets,data,cancel)
    timestamps=[p['create_time'] for p in data['items'] if p['create_time']]
    return {'name':name,'path':str(folder/'index.html'),'count':count,'format':'moments','resource_summary':data['resource_summary'],
            'scope':selection,'start_time':min(timestamps) if timestamps else None,'end_time':max(timestamps) if timestamps else None}
