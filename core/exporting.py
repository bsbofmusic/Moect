"""Bounded, cancellable export, derived from QCE's batch/writer pipeline."""
import csv
import hashlib
import html
import json
import re
from contextlib import closing, nullcontext
from datetime import datetime
from pathlib import Path
from posixpath import relpath
from urllib.parse import quote
from .streaming import BufferedTextWriter, json_line
from .resources import ResourceStore
from .version import VERSION,APP_NAME
from .export_options import export_preferences,is_system_message
from .themes import theme_archive,archive_viewer_template,archive_theme
from .inline_faces import ArchiveFaces
from wx4 import WECHAT_TIME_ZONE

FORMATS = {'html', 'json', 'jsonl', 'txt', 'csv', 'md', 'xlsx'}
MARKDOWN_INLINE = re.compile(r'([\\\x60*_{}\[\]<>!#&|~])')
MARKDOWN_BLOCK = re.compile(r'^([ ]{0,3})([=+-]|[0-9]{1,9}[.)])', re.MULTILINE)
MARKDOWN_INDENT = re.compile(r'^[ \t]+(?=[^ \t\n])', re.MULTILINE)
MARKDOWN_URL = re.compile(r"""https?://[^\s\x00-\x1f<>"`，。！？、；：“”]+""", re.IGNORECASE)
CSV_FORMULA = re.compile(r'^[\s\ufeff]*[=+\-@＝＋－＠]')
DECIMAL_ID = re.compile(r'-?[0-9]+')
WINDOWS_DEVICE_NAMES = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$',
                        *(prefix + digit for prefix in ('COM', 'LPT') for digit in '123456789¹²³')}


def safe_name(value):
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', value).strip(' .')[:70].rstrip(' .') or 'chat'
    if text.partition('.')[0].rstrip().upper() in WINDOWS_DEVICE_NAMES:
        text = '_' + text
    return text


def export_folder_name(name,uid,now=None):
    """Readable identity first, then a human-readable timestamp; never a bare UUID."""
    stamp=(now or datetime.now()).strftime('%Y-%m-%d_%H-%M-%S')
    return safe_name(str(name))[:36]+'_'+safe_name(str(uid))[:42]+'_'+stamp


def unused_folder(parent,name,suffix=''):
    candidate=parent/(name+suffix);number=2
    while candidate.exists():
        candidate=parent/(name+'_'+str(number)+suffix);number+=1
    return candidate


def clean(msg):
    if isinstance(msg,dict):
        return {k:clean(v) if isinstance(v, (dict, list)) else v
                for k,v in msg.items() if not k.startswith('_') and k not in ('packed_hex','content')}
    if isinstance(msg,list):return [clean(value) for value in msg]
    return msg


def message_tree(msg):
    yield msg
    if msg.get('quote'):
        yield from message_tree(msg['quote'])
    for child in (msg.get('forward') or {}).get('messages', []):
        yield from message_tree(child)


def portable_paths(msg,prefix):
    for item in message_tree(msg):
        if item.get('avatar'):
            item['avatar'] = prefix + item['avatar']
        for resource in item.get('resources', []):
            resource['path'] = prefix + resource['path']


def markdown_escape(value):
    text = MARKDOWN_INLINE.sub(r'\\\1', str(value))
    return MARKDOWN_BLOCK.sub(lambda m: m[1] + m[2][:-1] + '\\' + m[2][-1], text)


def markdown_text(value):
    value = value.replace('\r\n', '\n').replace('\r', '\n')
    parts = []
    start = 0
    for match in MARKDOWN_URL.finditer(value):
        url = match[0].rstrip('.,:;!?）】')
        for opening, closing in (('(', ')'), ('[', ']')):
            surplus = min(url.count(closing) - url.count(opening), len(url) - len(url.rstrip(closing)))
            if surplus > 0:
                url = url[:-surplus]
        if url.endswith('://'):
            continue
        parts.extend((markdown_escape(value[start:match.start()]), '<' + url + '>'))
        start = match.start() + len(url)
    parts.append(markdown_escape(value[start:]))
    escaped = MARKDOWN_INDENT.sub(lambda match: '&#160;' * len(match[0].expandtabs(4)), ''.join(parts))
    return escaped.replace('\n', '  \n')


def markdown_attachment(label, path, image=False):
    return ('!' if image else '') + '[' + markdown_escape(label) + '](<' + quote(path, safe='/') + '>)'


def readable_text(msg,markdown=False):
    text=msg.get('text') or ''
    extra=msg.get('extra') or {}
    game=extra.get('game_emoji')
    if markdown and game and text=='['+game['label']+']' and any(
            r['kind']=='image' and r['status']=='available' for r in msg.get('resources',[])):
        text=''
    url = extra.get('url')
    kind = msg.get('kind')
    if kind in ('link', 'miniprogram'):
        details = [extra.get('description')]
        if isinstance(url, str) and url.lower().startswith(('https://', 'http://')):
            details.append(url)
        if kind == 'miniprogram':
            mini = extra.get('miniprogram') or {}
            details.extend(mini.get(key) for key in ('appid', 'username', 'page_path'))
        for detail in details:
            if isinstance(detail, str) and detail and detail not in text:
                text += ('\n' if text else '') + detail
    display = markdown_escape if markdown else str
    if markdown:
        text = markdown_text(text)
    separator = '\n\n' if markdown else '\n'
    reply=msg.get('quote')
    if reply:
        reference=readable_text(reply,markdown)
        text+=separator+(('> ' if markdown else '↳ ')+display(reply.get('sender') or '')+': '+reference.replace('\n','\n> ' if markdown else '\n  '))
    forward=msg.get('forward')
    if forward:
        text+=separator+'【'+display(forward.get('title','转发的聊天记录'))+'】'
        for child in forward.get('messages',[]):
            text+=separator+'['+display(child.get('time_str',''))+'] '+display(child.get('sender',''))+('  \n' if markdown else '\n')+readable_text(child,markdown)
        if forward.get('notice'):text+=separator+display(forward['notice'])
    for resource in msg.get('resources',[]):
        path=resource['path']
        if markdown:
            label='图片' if resource['kind']=='image' else resource['name']
            text+=separator+markdown_attachment(label,path,resource['kind']=='image' and resource['status']=='available')
        else:text+='\n'+path
    if msg.get('resource_notice'):text+=separator+display(msg['resource_notice'])
    return text


def csv_cell(value):
    value = str(value)
    return "'" + value if CSV_FORMULA.match(value) else value


def write_html_chunks(folder, filename_prefix, records, progress=lambda _: None):
    chunks, parts, stamps = [], [], []
    size = count = 0

    def flush():
        filename = filename_prefix + '%06d.js' % (len(chunks)+1)
        with BufferedTextWriter(folder/filename) as writer:
            writer.write('window.__wxChunk=['+', '.join(parts)+'];')
        chunks.append(dict(file=filename,count=len(parts),start_time=min(stamps),end_time=max(stamps)))
        parts.clear()
        stamps.clear()

    for record in records:
        encoded = json.dumps(record, ensure_ascii=False)
        length = len(encoded.encode('utf-8'))
        if parts and (size+length>4*1024*1024 or len(parts)>=500):
            flush()
            size = 0
        parts.append(encoded)
        stamps.append(int(record.get('create_time') or 0))
        size += length
        count += 1
        if count % 200 == 0:
            progress(count)
    if parts:
        flush()
    return chunks, count


def materialize_forward(msg,folder,template,exported_at,cancel):
    """Write independent offline forward pages, plus complete JSON/TXT copies.

    Pages live beside the main HTML so media paths stay portable and cannot
    escape via '..'. Only explicitly referenced media are in the export folder.
    """
    if msg.get('quote'):materialize_forward(msg['quote'],folder,template,exported_at,cancel)
    forward=msg.get('forward')
    if not forward:return
    cancel();messages=forward.get('messages') or []
    identity=hashlib.sha256(json.dumps(forward,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:24]
    stem='forward_'+identity
    title=forward.get('title') or '转发的聊天记录'
    preview=[{'sender':m.get('sender',''),'text':m.get('text','')[:100]} for m in messages[:3]]
    meta={'application':APP_NAME,'version':VERSION,'name':title,
        'exported_at':exported_at,'count':len(messages),'forward':True,'notice':forward.get('notice','')}
    if not (folder/(stem+'.html')).exists():
        # Save full nested content before replacing child trees with page cards.
        with BufferedTextWriter(folder/(stem+'.json')) as writer:
            writer.write(json.dumps({'metadata':meta,'messages':messages},ensure_ascii=False))
        with BufferedTextWriter(folder/(stem+'.txt')) as writer:
            writer.write(title+'\n\n')
            for child in messages:
                cancel();writer.write('['+child.get('time_str','')+'] '+child.get('sender','')+'\n'+readable_text(child)+'\n\n')
            if forward.get('notice'):writer.write(forward['notice'])
        for child in messages:materialize_forward(child,folder,template,exported_at,cancel)
        def forward_records():
            for child in messages:
                cancel()
                yield child
        chunks, _ = write_html_chunks(folder, stem+'_c', forward_records())
        manifest={**meta,'chunks':chunks,'downloads':{'json':stem+'.json','txt':stem+'.txt'}}
        config=json.dumps(manifest,ensure_ascii=False).replace('<','\\u003c')
        (folder/(stem+'.html')).write_text(template.replace('__MANIFEST__',config),encoding='utf-8')
    msg['forward']={k:forward[k] for k in ('title','count','total','truncated','notice') if k in forward}
    msg['forward'].update(title=title,count=len(messages),preview=preview,
        html=stem+'.html',json=stem+'.json',txt=stem+'.txt')


def export_session(archive, session, options, folder, viewer_assets, progress, cancel, status=None,
                   scan_progress=None, *, batch=False, batch_media=None):
    fmt = options.get('format', 'html')
    if fmt not in FORMATS:
        raise ValueError('不支持的导出格式')
    title = session['display']
    stem = safe_name(title) + '_' + hashlib.sha256(session['username'].encode()).hexdigest()[:8]
    count = 0
    first_time = last_time = None
    out = (folder if batch else folder / stem) if fmt=='html' else folder / (stem+'_assets')
    out.mkdir(exist_ok=True)
    preferences=export_preferences(options)
    with (closing(ResourceStore(archive.data, session['username'], out, cancel, status, preferences,
                                remote_media=preferences['allow_remote_media'], batch_media=batch_media))
          if hasattr(archive, 'data') else nullcontext()) as resources:
        def records():
            nonlocal first_time,last_time
            processed = 0
            def on_scan(scanned):
                scan_progress(scanned, processed)
            scan = {'on_scan': on_scan} if scan_progress is not None else {}
            for msg in archive.messages(session['username'],options,cancel=cancel,**scan):
                if not preferences['include_system_messages'] and is_system_message(msg):continue
                first_time=msg['create_time'] if first_time is None else first_time
                last_time=msg['create_time']
                message = resources.enrich(msg) if resources else msg
                processed += 1
                yield message
        meta = dict(application=APP_NAME, version=VERSION, session=session['username'],
                    theme=archive_theme(options.get('_theme')),
                    name=title, exported_at=datetime.now(WECHAT_TIME_ZONE).replace(tzinfo=None).isoformat(timespec='seconds'),
                    time_zone=WECHAT_TIME_ZONE.tzname(None),
                    export_options=preferences,
                    demo=bool(options.get('demo')), filters={k: options[k] for k in ('query','kind','start','end') if k in options})
        # Like QCE's text exporter, source iteration is separate from rendering; each
        # record is released immediately after writing. HTML rotates bounded chunks.
        if fmt == 'xlsx':
            from .excel_export import write_excel
            path = folder/(stem + '.xlsx')
            def prepare(message):
                portable_paths(message, out.name + '/')
                attachments = list(dict.fromkeys(resource['path'] for item in message_tree(message)
                    for resource in item.get('resources', []) if resource.get('path')))
                return readable_text(message), attachments
            count = write_excel(path, records(), title, prepare, progress, cancel)
        elif fmt == 'html':
            faces = ArchiveFaces(viewer_assets, cancel)
            template = archive_viewer_template(viewer_assets, 'viewer.html', meta['theme'])
            if batch:
                link = '<div id="header-content"><p><a href="../index.html">返回会话目录</a></p>'
                template = template.replace('<div id="header-content">', link, 1)
            def html_records():
                for msg in records():
                    record=clean(msg)
                    faces.remember(record)
                    materialize_forward(record,out,template,meta['exported_at'],cancel)
                    yield record
            chunks, count = write_html_chunks(out, 'c', html_records(), progress)
            faces.write(out)
            manifest = dict(**meta, count=count, chunks=chunks,
                            resource_summary=resources.stats if resources else {})
            config = json.dumps(manifest, ensure_ascii=False).replace('<', '\\u003c')
            (out / 'index.html').write_text(template.replace('__MANIFEST__', config), encoding='utf-8')
            path = out / 'index.html'
        else:
            path = folder / (stem + '.' + fmt)
            with BufferedTextWriter(path, encoding='utf-8-sig' if fmt=='csv' else 'utf-8') as writer:
                if fmt=='json': writer.write('{"metadata":' + json.dumps(meta, ensure_ascii=False) + ',"messages":[')
                if fmt=='jsonl': writer.write(json_line({'metadata':meta}))
                if fmt in ('txt','md'):
                    heading = '# ' + markdown_escape(title) if fmt=='md' else title
                    writer.write(heading + '\n' + ('演示数据 · ' if meta['demo'] else '') + '由 '+APP_NAME+' 导出\n\n')
                if fmt=='csv':
                    csv_writer = csv.writer(writer)
                    csv_writer.writerow(['时间','会话','发送者','类型','内容','消息ID'])
                for msg in records():
                    # Non-HTML exports keep resources next to the document. JSON
                    # paths are relative to the document, not the assets directory.
                    prefix=out.name+'/'
                    portable_paths(msg,prefix)
                    if fmt=='json':
                        writer.write((',' if count else '') + json.dumps(clean(msg), ensure_ascii=False))
                    elif fmt=='jsonl': writer.write(json_line(clean(msg)))
                    elif fmt=='csv':
                        identifier = str(msg['server_id'])
                        fields = (msg['time_str'], title, msg['sender'], msg['type_name'], readable_text(msg))
                        csv_writer.writerow([*(csv_cell(v) for v in fields),
                                             identifier if DECIMAL_ID.fullmatch(identifier) else csv_cell(identifier)])
                    elif fmt=='md':
                        sender = markdown_escape(msg['sender'])
                        stamp = markdown_escape(msg['time_str'])
                        writer.write(f"[{stamp}] {sender}  \n{readable_text(msg,markdown=True)}\n\n")
                    else:
                        writer.write(f"[{msg['time_str']}] {msg['sender']}\n{readable_text(msg)}\n\n")
                    count += 1
                    if count % 200 == 0: progress(count)
                if fmt=='json': writer.write('],"count":'+str(count)+'}')
        cancel()
        progress(count)
        return dict(name=title, count=count, path=str(path), format=fmt,
                    start_time=first_time,end_time=last_time,
                    resource_summary=resources.stats if resources else {})


def export_index(folder, records, demo=False,theme=None, *, directory=False, failures=()):
    # Open HTML archives in the viewer; batch navigation stays inside it.
    if not directory and records and all(r['format']=='html' for r in records):
        paths=[Path(r['path']).relative_to(folder).as_posix() for r in records]
        if len(records)>1:
            for record,current in zip(records,paths):
                choices=''.join('<option value="%s"%s>%s</option>' % (
                    html.escape(quote(relpath(target,Path(current).parent.as_posix()),safe='/'),quote=True),
                    ' selected' if target==current else '',html.escape(item['name']))
                    for item,target in zip(records,paths))
                nav=('<nav aria-label="归档会话" style="margin-bottom:14px;font-size:12px;color:var(--muted-foreground)">'
                     '<label>切换会话 <select aria-label="切换会话" '
                     'style="font:inherit;color:var(--foreground);background:var(--card);border:1px solid var(--border);'
                     'border-radius:8px;padding:7px 10px;max-width:100%" '
                     'onchange="location.href=this.value">'+choices+'</select></label></nav>')
                path=Path(record['path'])
                content=path.read_text(encoding='utf-8')
                anchor='<div id="header-content">' if '<div id="header-content">' in content else '<header>'
                path.write_text(content.replace(anchor,anchor+nav,1),encoding='utf-8')
        target=quote(paths[0],safe='/')
        # Replace before paint, with a no-JS refresh and blocked-navigation link.
        content=('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                 '<title>'+html.escape(records[0]['name'])+' · Moect</title>'
                 '<meta http-equiv="refresh" content="0;url='+html.escape(target,quote=True)+'">'
                 '<script>location.replace('+json.dumps(target).replace('<','\\u003c')+');</script>'
                 '<a href="'+html.escape(target,quote=True)+'">打开聊天记录</a></html>')
        content=content.replace('<title>','<style>body{background:#f7f5fb;color:#292137}a{color:#7056c4}</style><title>',1)
        (folder/'index.html').write_text(theme_archive(content,theme),encoding='utf-8')
        return
    links = ''.join('<li><a href="%s">%s</a><span>%s 条 · %s</span></li>' %
                    (html.escape(quote(Path(r['path']).relative_to(folder).as_posix(),safe='/'), quote=True),
                     html.escape(r['name']), r['count'], r['format'].upper()) for r in records)
    content = ('<!doctype html><meta charset="utf-8">'
               '<meta name="viewport" content="width=device-width, initial-scale=1">'
               '<title>聊天记录归档</title><style>'
               'body{font:15px system-ui;max-width:850px;margin:70px auto;padding-inline:16px;background:#f7f5fb;color:#292137}'
               'h1{font-size:32px}ul{padding:0;list-style:none}'
               'li{display:flex;justify-content:space-between;gap:12px;padding:20px;background:white;margin:10px 0;border-radius:12px;overflow-wrap:anywhere}'
               'a{color:#7056c4;text-decoration:none}span{color:#7e718f;flex-shrink:0}'
               '</style>')
    content += '<h1>聊天记录归档' + (' · 演示数据' if demo else '') + '</h1><p>'+APP_NAME+' · 本地归档</p><ul>' + links + '</ul>'
    if failures:
        content += '<h2>未能导出的会话</h2><ul>' + ''.join(
            '<li>'+html.escape(name)+'</li>' for name in failures) + '</ul>'
    (folder/'index.html').write_text(theme_archive(content,theme), encoding='utf-8')
