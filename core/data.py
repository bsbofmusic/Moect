"""Read-only, bounded message access across WeChat 4.x database shards."""
import heapq
import itertools
import sqlite3
from pathlib import Path
from wx4.reader import Wx4Data
from .avatars import AvatarStore

FIELDS = ('local_id', 'server_id', 'local_type', 'sort_seq', 'real_sender_id',
          'create_time', 'message_content', 'compress_content', 'packed_info_data')


def time_bounds(start, end):
    conditions, args = [], []
    if start is not None:
        conditions.append('create_time >= ?')
        args.append(start)
    if end is not None:
        conditions.append('create_time <= ?')
        args.append(end)
    return conditions, args


class Archive:
    def __init__(self, library, *, cache_root=None, identity_from=None, session_ids=None):
        self.session_ids = session_ids
        self.data = Wx4Data(library['root'], library.get('wxid', ''),
                            account_dir=library.get('account_dir'), identity_from=identity_from)
        try:
            self.data.cache_root = cache_root or library.get('storage_root')
            for con in self.data._conns.values():
                if con:
                    con.execute('PRAGMA query_only=ON')
            self.avatars = AvatarStore.for_archive(self.data)
        except BaseException:
            self.data.close()
            raise

    def profile(self):
        me=self.data.me
        return {'name':me.get('nick_name') or me.get('remark') or '微信用户',
                'wxid':self.data.wxid, 'alias':me.get('alias') or self.data.wxid,
                'avatar_url':self.avatars.ui(self.data.wxid)}

    def close(self):
        self.data.close()

    def sessions(self):
        rows = self.data.sessions()
        for row in rows:
            row['category'] = self.data.session_category(row['username'])
            row['has_messages'] = self.data.has_messages(row['username'])
            row['avatar_url'] = self.avatars.reference(row['username'])
        return [r for r in rows if r['has_messages']]

    def has_session(self, username):
        if not isinstance(username, str) or not username:
            return False
        if self.session_ids is not None and username in self.session_ids:
            return self.data.has_messages(username)
        return any(row['username'] == username for row in self.sessions())

    def _raw(self, session, descending=False, before=None, start=None, end=None):
        table = self.data.msg_table_for(session)
        streams = []
        cursors = []
        for shard, con in enumerate(self.data.msg_dbs):
            if table not in self.data.tables_of(con):
                continue
            cols = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
            if not {'local_id', 'create_time', 'message_content'} <= cols:
                raise ValueError('消息表结构不受支持，请保留原始数据并更新适配层。')
            select = ','.join(f'"{c}"' if c in cols else f'NULL AS "{c}"' for c in FIELDS)
            clauses, args = time_bounds(start, end)
            if before is not None:
                ts, old_shard, local_id = before
                clauses.append('(create_time < ? OR (create_time = ? AND (? < ? OR (? = ? AND local_id < ?))))')
                args.extend([ts, ts, shard, old_shard, shard, old_shard, local_id])
            where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
            order = 'DESC' if descending else 'ASC'
            cursor = con.execute(f'SELECT {select} FROM "{table}"{where} ORDER BY create_time {order}, local_id {order}', args)
            cursors.append(cursor)
            names = self.data._name2id(con)
            def stream(cur=cursor, s=shard, ns=names):
                for row in cur:
                    yield ((row['create_time'] or 0, s, row['local_id'] or 0), row, ns)
            streams.append(stream())
        try:
            yield from heapq.merge(*streams, key=lambda item: item[0], reverse=descending)
        finally:
            for cursor in cursors:
                cursor.close()

    def messages(self, session, options=None, descending=False, before=None, cancel=None, on_scan=None):
        options = options or {}
        query = options.get('query', '').strip().casefold()
        kind = options.get('kind', 'all')
        rows = self._raw(session, descending, before, options.get('start'), options.get('end'))
        for scanned, (sort_key, row, names) in enumerate(rows):
            if cancel:
                cancel()
            if on_scan is not None:
                on_scan(scanned)
            msg = self.data._build_msg(row, names, session, sort_key[1])
            if kind != 'all' and msg.get('kind') != kind:
                continue
            if query and query not in (msg.get('text', '') + ' ' + msg.get('sender', '')).casefold():
                continue
            msg['_cursor'] = list(sort_key)
            # Never expose raw packed blobs or binary metadata through the UI.
            msg.pop('packed_hex', None)
            yield msg

    def page(self, session, options=None, before=None, limit=60):
        rows = []
        for msg in self.messages(session, options, descending=True, before=before):
            rows.append(msg)
            if len(rows) > limit:
                break
        more = len(rows) > limit
        rows = rows[:limit]
        return {'messages': list(reversed(rows)), 'more': more,
                'cursor': rows[-1]['_cursor'] if rows else None}

    def numbered_page(self, session, options, page, limit):
        offset=(page-1)*limit
        if options.get('query') or options.get('kind','all')!='all':
            result=[];total=0
            for msg in self.messages(session,options,descending=True):
                if offset<=total<offset+limit: result.append(msg)
                total+=1
            return result,total
        total = self.message_count(session, options)
        result=[]
        source=self._raw(session,True,None,options.get('start'),options.get('end'))
        try:
            for sort_key,row,names in itertools.islice(source,offset,offset+limit):
                result.append(self.data._build_msg(row,names,session,sort_key[1]))
        finally:source.close()
        return result,total

    def message_count(self, session, options, cancel=lambda: None):
        """Count source rows in the date range without parsing message bodies."""
        table = self.data.msg_table_for(session)
        clauses, args = time_bounds(options.get('start'), options.get('end'))
        condition = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        total = 0
        for con in self.data.msg_dbs:
            cancel()
            if table in self.data.tables_of(con):
                total += con.execute(f'SELECT count(*) FROM "{table}"{condition}', args).fetchone()[0]
        return total


def validate_root(root):
    path = Path(root).resolve()
    session = list(path.rglob('session.db'))
    messages = list(path.rglob('message_*.db'))
    if not session or not messages:
        raise ValueError('请选择包含 session、contact、message 子目录的已解密备份。')
    try:
        with open(session[0], 'rb') as f:
            if f.read(16) != b'SQLite format 3\x00':
                raise ValueError('此目录还是加密数据库，请使用“连接微信”导入。')
        con = sqlite3.connect(session[0].as_uri() + '?mode=ro', uri=True)
        try:
            con.execute('SELECT 1 FROM SessionTable LIMIT 1').fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        raise ValueError('备份数据库无法读取或格式不兼容。') from e
    return str(path)
