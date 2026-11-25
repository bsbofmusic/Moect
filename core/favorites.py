"""Read-only WeChat 4.x favorites, with verified account-local attachments.

Schema and opaque-cache behavior were cross-checked against wechat-cli and
WeChatDataAnalysis, then verified against a local 4.x snapshot. No UI automation
or client/network protocol emulation is used.
"""
import hashlib
import json
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from contextlib import closing
from itertools import islice
from pathlib import Path
from urllib.parse import urlsplit

from .account_assets import open_asset_db, tables, integer, text
from .account_exporting import collection_media, metadata
from .exporting import clean, markdown_attachment, markdown_escape, markdown_text, safe_name
from .resources import ResourceStore
from .themes import archive_theme, archive_viewer_template
from .paths import is_link
from .inline_faces import ArchiveFaces
from .streaming import json_line, write_archive_data
from .game_emojis import game_image
from wx4.game_emojis import from_hash
from wx4.media import SIG_V1, SIG_V2, decode_dat, image_prefix

TYPES = {1:'文本',2:'图片',3:'语音',4:'视频',5:'链接 / 文章',6:'位置',7:'音乐',8:'文件',
         14:'聊天记录',16:'商品',18:'笔记',19:'小程序',20:'视频号',37:'表情'}
DIRECT = {1:'text',2:'image',3:'voice',4:'video',5:'link',6:'location',7:'link',8:'file',19:'link',20:'link',37:'image'}
RECORD = {1:'text',2:'image',3:'contact',4:'voice',5:'video',6:'link',7:'location',8:'file',17:'record',19:'link',22:'link',37:'image'}
ATTACHMENTS = {'image','voice','video','file'}
MD5 = re.compile(r'^[a-fA-F0-9]{32}$')
NOTICE = '先在电脑微信打开需要的收藏及附件，再刷新。仅导出本机已有内容；公众号正文可在导出时尝试联网保存。'


def web_url(value):
    value = text(value).strip()
    try:
        url = urlsplit(value)
        if len(value) <= 8192 and not any(ord(c)<32 for c in value) and url.scheme in ('http','https') and url.hostname and not url.username:
            return value
    except ValueError:
        pass
    return ''


def xml_root(value):
    value = text(value)
    if len(value) > 16*1024*1024 or re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b',value,re.I):
        return None
    try:
        return ET.fromstring(value)
    except (ET.ParseError, ValueError):
        return None


def value(node, *paths):
    for path in paths:
        found = node.findtext(path)
        if found and found.strip():
            return found.strip()
    return ''


def parse_favorite(row, name_of=lambda uid:uid):
    typ = integer(row['type']) & 0xffffffff
    item = {'id':str(row['local_id']), 'type':typ, 'type_name':TYPES.get(typ,'其他收藏'),
            'create_time':integer(row['update_time']), 'title':'', 'text':'', 'link':'', 'blocks':[], 'status':'available'}
    origin = text(row['realchatname'] or row['fromusr'])
    item['source'] = name_of(origin) if origin else ''
    raw = text(row['content'])
    root = xml_root(raw)
    if root is not None and root.tag!='favitem':
        root=root.find('.//favitem')
    if root is None:
        if typ == 1 and not raw.lstrip().startswith('<'):
            item['text'] = raw
        else:
            item.update(status='unsupported',text='这条收藏的本地结构暂未解析。')
    else:
        item['title'] = value(root,'title','weburlitem/pagetitle','appbranditem/sourcedisplayname','.//finderFeed/nickname')
        item['text'] = value(root,'desc','weburlitem/pagedesc','locitem/poiname','locitem/label','.//finderFeed/desc')
        item['link'] = web_url(value(root,'weburlitem/clean_url','weburlitem/pageurl','source/link','link'))
        # The enclosing message ID identifies a standalone voice, not a voice
        # embedded in a note or forwarded record.
        default_server = value(root,'source/msgid','source/sourceid') if typ==3 else ''

        def block(node, depth=0):
            kind = (RECORD if typ==14 or depth else DIRECT).get(integer(node.get('datatype')),'other')
            fmt = value(node,'datafmt').lower().lstrip('.')
            # Old favorites and newer record envelopes can use different enums.
            if fmt in ('silk','amr','wav','mp3','m4a'):kind='voice'
            elif fmt in ('mp4','mov','webm') or (integer(node.get('datatype'))==4 and value(node,'stream_weburl','stream_dataurl')):kind='video'
            elif node.find('weburlitem') is not None:kind='link'
            elif node.find('locitem') is not None:kind='location'
            sender=value(node,'datasrcname')
            if not sender:
                uid=value(node,'dataitemsource/realchatname','dataitemsource/fromusr')
                if uid:sender=name_of(uid)
            result = {'kind':kind, 'title':value(node,'datatitle','weburlitem/pagetitle','locitem/poiname','finderFeed/nickname'),
                      'text':value(node,'datadesc','weburlitem/pagedesc','locitem/label','finderFeed/desc'),
                      'sender':sender, 'time':value(node,'datasrctime'),
                      'link':web_url(value(node,'weburlitem/clean_url','weburlitem/pageurl','stream_weburl','link')),
                      'resources':[], '_media':{tag:value(node,tag) for tag in
                        ('fullmd5','thumbfullmd5')},
                      '_format':fmt,
                      '_server':value(node,'fromnewmsgid') or (default_server if depth==0 else '')}
            nested = node.find('recorditem')
            if nested is not None and depth<6:
                result['kind']='record'
                inner = xml_root(nested.text) if nested.text and nested.text.lstrip().startswith('<') else nested
                if inner is not None:
                    result['children']=[block(child,depth+1) for child in inner.findall('./datalist/dataitem')]
            return result

        nodes = root.findall('./datalist/dataitem')
        item['blocks'] = [block(node) for node in nodes]
    if not item['title']:
        item['title'] = next((b['title'] for b in item['blocks'] if b['title']), '') or item['text'].split('\n')[0][:90] or item['type_name']+'收藏'
    return item


class FavoriteCatalog:
    def __init__(self, library, name_of=lambda uid:uid):
        self.library,self.name_of = library,name_of

    def _rows(self, con, options, cancel=lambda:None, *, ids_only=False):
        if 'fav_db_item' not in tables(con):
            raise ValueError('当前收藏库结构暂不受支持，请重新连接微信或更新适配。')
        where,args = ['COALESCE(flag,0)=0'],[]
        for field,op in (('_start','>='),('_end','<=')):
            if options.get(field) is not None:where.append('update_time '+op+' ?');args.append(options[field])
        typ = text(options.get('favoriteType','all'))
        if typ != 'all':
            if not typ.isdigit():raise ValueError('收藏类型无效')
            where.append('(type & 4294967295)=?');args.append(int(typ))
        wanted = options.get('ids')
        excluded = options.get('exclude_ids',[])
        if wanted is not None and (not isinstance(wanted,list) or not all(isinstance(v,str) and v.isdigit() for v in wanted)):
            raise ValueError('收藏选择无效')
        if not isinstance(excluded,list) or not all(isinstance(v,str) and v.isdigit() for v in excluded):raise ValueError('收藏选择无效')
        wanted = set(wanted) if wanted is not None else None
        excluded = set(excluded)
        if wanted is not None and not wanted:
            return
        if wanted and len(wanted)<=400:
            where.append('local_id IN ('+','.join('?' for _ in wanted)+')')
            args.extend(wanted)
        columns = 'local_id' if ids_only else 'local_id,type,update_time,content,fromusr,realchatname'
        sql = 'SELECT '+columns+' FROM fav_db_item WHERE '+' AND '.join(where)
        if not ids_only:
            sql += ' ORDER BY update_time DESC,local_id DESC'
        with closing(con.execute(sql,args)) as rows:
            for row in rows:
                cancel();identity=str(row['local_id'])
                if identity in excluded or wanted is not None and identity not in wanted:continue
                yield row

    def count(self, options, cancel):
        con = open_asset_db(self.library,'favorite/favorite.db')
        if con is None:return 0
        with closing(con):
            return sum(1 for _ in self._rows(con,options,cancel,ids_only=True))

    def _matching_items(self, rows, query):
        for row in rows:
            item = parse_favorite(row,self.name_of)
            if not query or query in json.dumps(clean(item),ensure_ascii=False).casefold():
                yield item

    def items(self, options=None, cancel=lambda:None):
        options = options or {}
        con = open_asset_db(self.library,'favorite/favorite.db')
        if con is None:return
        with closing(con), closing(self._rows(con,options,cancel)) as rows:
            yield from self._matching_items(rows,text(options.get('query')).strip().casefold())

    def summary(self, options=None):
        options=options or {};offset=max(0,integer(options.get('offset')));limit=80;selected=[]
        con=open_asset_db(self.library,'favorite/favorite.db')
        if con is None:return {'available':False,'notice':'尚无收藏快照。先在电脑微信打开“收藏”，再重新连接并整理微信。','items':[],'count':0}
        query=text(options.get('query')).strip().casefold()
        with closing(con), closing(self._rows(con,options)) as rows:
            if query:
                count=0
                for item in self._matching_items(rows,query):
                    if offset<=count<offset+limit:selected.append(item)
                    count+=1
            else:
                count=sum(1 for _ in self._rows(con,options,ids_only=True))
                selected=list(self._matching_items(islice(rows,offset,offset+limit),''))
        items=[clean({key:value for key,value in item.items() if key!='blocks'}) for item in selected]
        for item in items:
            item['text']=item['text'][:600]
        return {'available':True,'notice':NOTICE,'items':items,'count':count,'offset':offset,'limit':limit,
                'types':[{'id':str(k),'name':v} for k,v in TYPES.items()]}


class FavoriteResources:
    """Index only the favorite cache; opaque names never establish identity."""
    def __init__(self, store, library, seconds=None, *, index_from=None):
        self.store,self.library=store,library
        self.deadline=time.monotonic()+seconds if seconds is not None else None
        self.indexed=False
        self.voice_databases=None
        self.index_from=index_from
        if index_from is None:
            store.index.execute('CREATE TABLE favorite_files (md5 TEXT,path TEXT,role TEXT,needs_verification INTEGER)')
            store.index.execute('CREATE INDEX favorite_md5 ON favorite_files(md5)')
        # Recovery is an explicit shared UI action, never repeated per cache file.
        store.keys.scan_remaining=0

    def index(self):
        if self.indexed:return
        self.indexed=True;store=self.store
        if self.index_from is not None:
            store.index.execute('ATTACH DATABASE ? AS favorite_cache', (self.index_from.as_uri()+'?mode=ro',))
            return
        base=Path(store.data.account_dir()).resolve()/'business/favorite'
        if not base.is_dir() or is_link(base):return
        paths=[]
        for bucket in ('data','mid','thumb','temp'):
            if self.deadline is not None and time.monotonic()>self.deadline:break
            directory=base/bucket
            if is_link(directory):continue
            for current,dirs,files in os.walk(directory,followlinks=False):
                if self.deadline is not None and time.monotonic()>self.deadline:break
                store.cancel();dirs[:]=[n for n in dirs if not is_link(Path(current)/n)]
                for name in files:
                    if self.deadline is not None and time.monotonic()>self.deadline:break
                    p=Path(current)/name
                    if not is_link(p) and p.resolve().is_relative_to(base):paths.append((p,bucket))
        samples=[p for p,_ in paths[:40]]
        for path,bucket in paths:
            store.cancel()
            if self.deadline is not None and time.monotonic()>self.deadline:break
            try:
                source = path
                size = path.stat().st_size
                large = size>128*1024*1024
                if large and self.deadline is not None:continue
                with path.open('rb') as src:
                    head=src.read(16)
                    dat=head.startswith((SIG_V1,SIG_V2))
                    if large and dat:continue
                    if not large and (dat or (not image_prefix(head) and decode_dat(head) is not None)):
                        src.seek(0)
                        raw=src.read(size+1)
                        if len(raw)!=size:continue
                        decoded=store.keys.decode(raw,path,samples)
                        if decoded is None and dat:continue
                        digest=hashlib.md5(decoded if decoded is not None else raw)
                        if decoded is not None and decoded is not raw:
                            source=Path(store.tmp.name)/('favorite-'+hashlib.sha256(str(path).encode()).hexdigest())
                            source.write_bytes(decoded)
                    else:
                        digest=hashlib.md5(head)
                        while chunk := src.read(1024*1024):
                            store.cancel();digest.update(chunk)
                role = 'thumbnail' if not large and bucket in ('thumb','mid') else 'main'
                store.index.execute('INSERT INTO favorite_files VALUES (?,?,?,?)',
                                    (digest.hexdigest(),str(source),role,source==path))
            except (OSError,ValueError):continue
        store.index.commit()

    def verified_source(self, path, digest):
        """Take a stable copy only after an attachment was selected by its content hash."""
        store = self.store
        target = Path(store.tmp.name)/('verified-'+digest)
        if target.is_file():
            return target
        partial = target.with_suffix('.part')
        check = hashlib.md5()
        try:
            if is_link(path) or path.resolve() != path:
                return None
            with path.open('rb') as original, partial.open('wb') as output:
                while chunk := original.read(1024*1024):
                    store.cancel()
                    check.update(chunk)
                    output.write(chunk)
            if check.hexdigest() == digest:
                partial.replace(target)
                return target
        finally:
            partial.unlink(missing_ok=True)
        return None

    def resolve(self, block):
        store=self.store;kind=block['kind'];hints=block['_media']
        if kind not in ATTACHMENTS:return
        if kind=='image' and (game:=from_hash(hints['fullmd5'])):
            image=game_image(game)
            if image:
                block['resources'].append(store._copy('wechat-game:'+game['md5'],'image',image,'.png'))
                return
        candidates=[]
        for field,role in (('fullmd5','main'),('thumbfullmd5','thumbnail')):
            digest=hints[field].lower()
            if not MD5.fullmatch(digest):continue
            self.index()
            for path,stored_role,needs_verification in store.index.execute(
                    'SELECT path,role,needs_verification FROM favorite_files WHERE md5=? ORDER BY role', (digest,)):
                if role=='main' and stored_role!='main':continue
                try:
                    source = self.verified_source(Path(path), digest) if needs_verification else Path(path)
                except OSError:
                    continue
                if source:
                    candidates.append((source,role,digest))
        voice_seen=set()
        for path,role,digest in candidates:
            store.cancel();resource=None
            try:
                if kind=='image' or role=='thumbnail':
                    if path.stat().st_size>128*1024*1024:continue
                    blob=path.read_bytes()
                    image=store.codec.image(blob)
                    if image:
                        from .avatars import image_type
                        resource=store._copy(digest,'image',image,'.'+image_type(image)[0],role=role)
                elif kind=='voice':
                    if path.stat().st_size>16*1024*1024:continue
                    blob=path.read_bytes()
                    content_hash=hashlib.sha256(blob).hexdigest()
                    if content_hash in voice_seen:continue
                    voice_seen.add(content_hash)
                    resource=self.export_voice(blob,digest)
                elif kind=='video':
                    converted=store.codec.video(path,Path(store.tmp.name)/'favorite-video.mp4')
                    if converted:resource=store._copy(converted,'video',extension='.mp4',identity_source=digest)
                else:
                    ext=re.sub(r'[^a-zA-Z0-9]','',block.get('_format',''))[:12]
                    resource=store._copy(path,'file',extension='.'+ext if ext else '.bin',identity_source=digest)
                    resource['name']=block['title'] or ('收藏文件'+('.'+ext if ext else ''))
                if resource:
                    block['resources'].append(resource)
                    if role=='main':return
                    break
            except (OSError,ValueError):continue
        original=next(((p,d) for p,r,d in candidates if r=='main'),None)
        original_voice=None
        if kind=='voice':
            # A cache entry can be damaged while a different snapshot still
            # has a playable copy of the same message.
            with closing(self.voice_candidates(block,voice_seen)) as voices:
                for blob,digest in voices:
                    if original is None and original_voice is None:
                        original_voice=blob,digest
                    try:resource=self.export_voice(blob,digest)
                    except (OSError,ValueError):continue
                    if resource:
                        block['resources'].append(resource)
                        return
        if original:
            path,digest=original
            fmt=re.sub(r'[^a-zA-Z0-9]','',block.get('_format',''))[:12] or 'bin'
            ext='.silk' if kind=='voice' else '.'+fmt
            block['resources'].append(store._copy(path,'original',extension=ext,status='unsupported',identity_source=digest))
        elif original_voice is not None:
            blob,digest=original_voice
            block['resources'].append(store._copy(digest,'original',blob,'.silk',status='unsupported'))
        else:
            store.stats['missing']+=1
            block['notice']='本机未找到可用原文件。请在电脑微信打开或下载这条收藏的附件，再刷新并导出。'
            return
        block['notice']='原始附件已保留，但未能转换为浏览器可播放的格式。'

    def export_voice(self, blob, identity):
        store=self.store
        converted=store.codec.audio(blob,Path(store.tmp.name)/'favorite-voice.wav')
        if converted is None:return None
        resource=store._copy(converted,'audio',extension='.wav',identity_source=identity)
        resource['name']=identity+'.wav'
        return resource

    def voice_candidates(self, block, seen):
        server=block.get('_server','')
        if not re.fullmatch(r'-?[0-9]{1,20}',server):return
        server=int(server)
        if not server or not -(1<<63)<=server<1<<64:return
        if server>=1<<63:server-=1<<64
        if self.voice_databases is None:
            root=Path(self.library['root'])
            assets=Path(self.library.get('assets_root') or root)
            self.voice_databases=[(base,path.relative_to(base).as_posix())
                for base in dict.fromkeys((assets,root)) for path in sorted((base/'message').glob('media_*.db'))]
        checked=0
        for root,relative in self.voice_databases:
            self.store.cancel()
            # The same shard name can exist in both snapshots with different
            # contents. Read each concrete database in order.
            try:con=open_asset_db({'root':root},relative)
            except (OSError,ValueError):continue
            if con is None:continue
            try:
                if 'VoiceInfo' not in tables(con):continue
                for blob, in con.execute('SELECT voice_data FROM VoiceInfo WHERE svr_id=? AND length(voice_data) BETWEEN 1 AND 16777216 ORDER BY create_time DESC LIMIT 8',(server,)):
                    self.store.cancel()
                    if not isinstance(blob,bytes):continue
                    digest=hashlib.sha256(blob).hexdigest()
                    if digest in seen:continue
                    seen.add(digest)
                    yield blob,digest
                    checked+=1
                    if checked>=8:return
            except sqlite3.Error:continue
            finally:con.close()


def markdown(item):
    esc = markdown_escape
    lines=['# '+esc(item['title']),'',f"收藏类型：{item['type_name']}",f"来源：{esc(item['source'])}",'']
    if item['link']:lines.extend(['原链接：<'+item['link'].replace('>','%3E')+'>',''])
    if item['text']:lines.extend([markdown_text(item['text']),''])
    def write(block):
        if block.get('sender'):lines.extend(['**'+esc(block['sender'])+'** '+esc(block.get('time','')),''])
        if block.get('title'):lines.extend([esc(block['title']),''])
        if block.get('text'):lines.extend([markdown_text(block['text']),''])
        if block.get('link'):lines.extend(['<'+block['link'].replace('>','%3E')+'>',''])
        for resource in block.get('resources',[]):
            label=resource.get('name') or resource['kind']
            lines.extend([markdown_attachment(label,'../'+resource['path'],resource['kind']=='image' and resource['status']=='available'),''])
        if block.get('notice'):lines.extend(['> '+esc(block['notice']),''])
        for child in block.get('children',[]):write(child)
    for block in item['blocks']:write(block)
    if item.get('article_notice'):lines.extend(['> '+esc(item['article_notice']),''])
    return '\n'.join(lines)


def export_favorites(archive,library,payload,folder,assets,progress,cancel,status):
    from .favorite_articles import fetch_article
    articles=payload.get('articles',True)
    if not isinstance(articles,bool):raise ValueError('文章下载选项无效')
    catalog=FavoriteCatalog(library,archive.data.name_of)
    searching=bool(text(payload.get('query')).strip())
    status('正在筛选待导出的收藏…')
    total=0 if searching else catalog.count(payload,cancel)
    if not total and not searching:
        raise ValueError('所选范围没有可导出的收藏，请刷新后重新选择。')
    profile=archive.profile()
    data=metadata(profile['name']+' · 收藏','favorites',profile)
    data.update(items=[],theme=archive_theme(payload.get('_theme')),notice=NOTICE)
    store=ResourceStore(archive.data,'',folder,cancel,status,{'includeAvatars':False})
    resolver=FavoriteResources(store,library)
    faces=ArchiveFaces(assets,cancel)
    md=folder/'Markdown'

    def attachments(blocks):
        for block in blocks:
            if block['kind'] in ATTACHMENTS:yield block
            yield from attachments(block.get('children',()))

    def with_media(worker):
        def resolve(item):
            for block in attachments(item['blocks']):
                cancel()
                worker.resolve(block)
            return item
        return resolve

    def prepare(items):
        # Hash the account's opaque cache once. Every worker reads the same finished index.
        if not resolver.indexed and any(
            MD5.fullmatch(digest) for item in items for block in attachments(item['blocks'])
            for digest in block['_media'].values()
        ):
            status('正在查找本机收藏附件…')
            resolver.index()
        status('正在处理收藏附件…')

    index = Path(store.tmp.name)/'index.db'
    with closing(store), closing(catalog.items(payload,cancel)) as source, closing(collection_media(
        source,store,with_media(resolver),lambda worker: with_media(FavoriteResources(worker,library,index_from=index)),
        prepare,lambda item: any(attachments(item['blocks'])),status,
    )) as entries:
        md.mkdir()
        article_at=0
        count=0
        for count,item in enumerate(entries,1):
            if item['type']==5 and articles:
                is_article=(urlsplit(item['link']).hostname or '').lower()=='mp.weixin.qq.com'
                while is_article and time.monotonic()-article_at<.5:
                    cancel();time.sleep(.05)
                article,notice=fetch_article(item['link'],store)
                if is_article:article_at=time.monotonic()
                item['article_notice']=notice
                if article:item['blocks'].extend(article)
            elif item['type']==5:
                item['article_notice']='未勾选联网保存正文，已保存标题、摘要和原链接。'
            for block in item['blocks']:
                # Notes store their rich text in a small HTML attachment.
                if block['kind']=='file' and block.get('_format') in ('htm','html') and item['type']==18:
                    from .favorite_articles import local_note
                    for resource in block['resources']:
                        if resource['kind']=='file':
                            extra=local_note((folder/resource['path']).read_bytes())
                            if extra:item['blocks'].extend(extra)
                    break
            cleaned=clean(item)
            faces.remember_text(cleaned['text'])
            def remember(block):
                faces.remember_text(block.get('text'))
                for child in block.get('children',[]):remember(child)
            for block in cleaned['blocks']:remember(block)
            filename=item['id']+'_'+safe_name(item['title'])[:65]+'.md'
            (md/filename).write_text(markdown(cleaned),encoding='utf-8')
            cleaned['markdown']='Markdown/'+filename;data['items'].append(cleaned)
            store.report.write(json_line({'id':item['id'],'article':item.get('article_notice',''),
                'missing':sum(bool(b.get('notice')) for b in item['blocks'])}))
            progress(count,total)
        data['resource_summary']=dict(store.stats)
    cancel()
    if not count:raise ValueError('所选范围没有可导出的收藏，请刷新后重新选择。')
    progress(count,count)
    status('正在写入收藏归档…')
    faces.write(folder)
    template=archive_viewer_template(assets,'favorites.html',data['theme'])
    write_archive_data(folder,template,'__FAVORITES__',data,cancel)
    return {'name':data['name'],'path':str(folder/'index.html'),'count':count,'format':'favorites','resource_summary':data['resource_summary']}
