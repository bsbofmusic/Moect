"""Synthetic retained conversations, batch queues and offline printing regressions."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.account_exporting import write_collection
from core.data import Archive, validate_root
from core.exporting import export_index, export_session
from core.service import Service
from core.themes import archive_viewer_template
from tests.test_core import fixture, SESSION
from tests.test_resources import ASSETS, PNG
from wx4.decrypt import quick_check


class RetainedConversationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)/'中文资料 # 100% & 空格'
        self.library = fixture(self.root/'数据库', count=4)

    def archive(self):
        archive = Archive(self.library)
        self.addCleanup(archive.close)
        return archive

    def test_appmsg_text_and_reply_do_not_borrow_nested_link_metadata(self):
        archive = self.archive()
        data = archive.data
        def message(content, raw_type=49):
            row = dict(local_id=10, server_id='100', local_type=raw_type, sort_seq=10,
                       real_sender_id=2, create_time=1800000000, message_content=content,
                       compress_content=None, packed_info_data=None)
            return data._build_msg(row, {2: 'friend'}, SESSION, 0)
        text = '标题和普通网址 https://example.com/video?id=1'
        result = message('<msg><appmsg><type>1</type><title>' + text + '</title></appmsg></msg>')
        self.assertEqual((result['kind'], result['text'], result['extra']), ('text', text, {}))
        self.assertEqual(message(text)['text'], text)
        quote = '<appmsg><title>半箱牛奶</title><refermsg><type>5</type><displayname>朋友</displayname><content>12块钱</content><url>https://example.com/quoted</url></refermsg></appmsg>'
        for raw_type in (49, (57 << 32) | 49):
            result = message(quote, raw_type)
            self.assertEqual((result['kind'], result['text']), ('quote', '半箱牛奶'))
            self.assertEqual(result['extra']['url'], '')
            self.assertEqual(result['_quote']['text'], '12块钱')
        link = message('<appmsg><type>5</type><title>实际分享</title><url>https://example.com/real</url></appmsg>')
        self.assertEqual((link['kind'], link['extra']['url']), ('link', 'https://example.com/real'))

    def retain_friend(self, unknown=False):
        group_table = 'Msg_' + hashlib.md5(SESSION.encode()).hexdigest()
        table = 'Msg_' + hashlib.md5(b'friend').hexdigest()
        database = Path(self.library['root'])
        with closing(sqlite3.connect(database/'contact/contact.db')) as connection, connection:
            connection.execute("DELETE FROM contact WHERE username='friend'")
        for shard in range(2):
            with closing(sqlite3.connect(database/'message'/f'message_{shard}.db')) as connection, connection:
                connection.execute(f'CREATE TABLE "{table}" AS SELECT * FROM "{group_table}"')
                connection.execute(f'UPDATE "{table}" SET create_time=?, message_content=?', (1800000100 + shard, '保留的历史文字'))
                if unknown:
                    connection.execute("DELETE FROM Name2Id WHERE user_name='friend'")
        return table

    def test_deleted_friend_is_discovered_previewed_and_exported_from_message_tables(self):
        self.retain_friend()
        archive = self.archive()
        sessions = archive.sessions()
        retained = next(row for row in sessions if row['username'] == 'friend')
        self.assertEqual(retained['last_time'], 1800000101)
        self.assertEqual(retained['summary'], '保留的历史文字')
        self.assertEqual(len(list(archive.messages('friend'))), 4)
        out = self.root/'中文导出';out.mkdir()
        result = export_session(archive, retained, {'format': 'html', 'quickExport': True}, out, ASSETS, lambda _: None, lambda: None)
        self.assertEqual(result['count'], 4)
        self.assertTrue(Path(result['path']).is_file())

    def test_unmapped_message_table_remains_exportable_with_a_local_identity(self):
        table = self.retain_friend(unknown=True)
        archive = self.archive()
        retained = next(row for row in archive.sessions() if row['username'].startswith('local-msg:'))
        self.assertIn('本地会话', retained['display'])
        self.assertEqual(archive.data.msg_table_for(retained['username']), table)
        rows = list(archive.messages(retained['username']))
        self.assertEqual(len(rows), 4)
        attachment = Path(self.library['root']).parent/'msg/attach'/table[4:]
        attachment.mkdir(parents=True)
        self.assertEqual(Path(archive.data.attach_dir(retained['username'])), attachment)

    def test_retained_fallback_message_types_do_not_abort_session_loading(self):
        table = self.retain_friend(unknown=True)
        database = Path(self.library['root'])
        cases = [(37, '好友请求'), (51, '状态'), (66, '连线'),
                 (255, '其他(255)'), ((2 << 32) | 51, '状态')]
        for raw_type, label in cases:
            with self.subTest(raw_type=raw_type):
                for shard in range(2):
                    with closing(sqlite3.connect(database/'message'/f'message_{shard}.db')) as connection, connection:
                        connection.execute(f'UPDATE "{table}" SET local_type=?', (raw_type,))
                archive = Archive(self.library)
                try:
                    sessions = archive.sessions()
                    self.assertEqual(len(sessions), 2)
                    retained = next(row for row in sessions if row['username'].startswith('local-msg:'))
                    self.assertEqual(retained['summary'], '[' + label + ']')
                    rows = list(archive.messages(retained['username']))
                    self.assertEqual(len(rows), 4)
                    self.assertTrue(all(row['type_name'] == label for row in rows))
                    self.assertTrue(all(row['content'] == '保留的历史文字' for row in rows))
                finally:
                    archive.close()

    def test_chinese_and_reserved_path_characters_work_for_readonly_sqlite(self):
        self.assertEqual(validate_root(self.library['root']), str(Path(self.library['root']).resolve()))
        self.assertTrue(quick_check(str(Path(self.library['root'])/'session/session.db')))
        archive = self.archive()
        self.assertEqual(len(archive.sessions()), 1)
        self.assertEqual(len(list(archive.messages(SESSION))), 4)

    def test_session_table_identity_is_not_duplicated_when_contact_metadata_is_missing(self):
        database = Path(self.library['root'])
        with closing(sqlite3.connect(database/'contact/contact.db')) as connection, connection:
            connection.execute('DELETE FROM contact WHERE username=?', (SESSION,))
        for shard in range(2):
            with closing(sqlite3.connect(database/'message'/f'message_{shard}.db')) as connection, connection:
                connection.execute('DELETE FROM Name2Id WHERE user_name=?', (SESSION,))
        archive = self.archive()
        self.assertEqual([row['username'] for row in archive.sessions()], [SESSION])

    def test_retained_voice_uses_an_unambiguous_server_id_when_name_mapping_is_gone(self):
        path = Path(self.library['root'])/'message/media_0.db'
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.executescript('CREATE TABLE Name2Id(user_name TEXT); CREATE TABLE VoiceInfo(chat_name_id INTEGER,svr_id INTEGER,create_time INTEGER,voice_data BLOB);')
            connection.execute('INSERT INTO VoiceInfo VALUES(9,12345,1,?)', (b'synthetic-voice',))
            connection.execute('INSERT INTO VoiceInfo VALUES(9,12345,2,?)', (b'synthetic-voice',))
            connection.executemany('INSERT INTO VoiceInfo VALUES(9,67890,1,?)', [(b'first',), (b'conflicting',)])
        archive = self.archive()
        self.assertEqual(archive.data.voice_blob('removed-friend', '12345'), b'synthetic-voice')
        self.assertIsNone(archive.data.voice_blob('removed-friend', '67890'))
        self.assertIsNone(archive.data.voice_blob('removed-friend', '0'))


class BatchQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service = Service(ASSETS, self.root/'state')
        self.addCleanup(self.service.shutdown)
        self.service.library = {'name': 'Synthetic archive', 'root': 'synthetic', 'wxid': 'synthetic'}
        self.service.config['export_dir'] = str(self.root/'exports')
        self.rows = [dict(username='synthetic_' + str(index), display='会话 ' + str(index)) for index in range(125)]
        self.archive = SimpleNamespace(sessions=lambda: self.rows, close=lambda: None, message_count=lambda *_: 1)

    def test_more_than_80_conversations_create_one_cancellable_batch(self):
        with patch.object(self.service, 'archive', return_value=self.archive), patch.object(self.service.pool, 'submit') as submit:
            response = self.service.export({'sessions': [row['username'] for row in self.rows]})
        submit.assert_called_once()
        run, job, work = submit.call_args.args
        saved = json.loads(self.service.config_path.read_text(encoding='utf-8'))
        self.assertEqual(len(saved['tasks']), 1)
        self.assertEqual(saved['tasks'][0]['session_total'], 125)
        public = self.service.state()['tasks'][0]
        self.assertTrue(public['has_session_progress'])
        self.assertNotIn('session_progress', public)
        progress = self.service.task_session_progress({'id':response['job'], 'offset':120})
        self.assertEqual(progress['total'], 125)
        self.assertEqual([row['status'] for row in progress['rows']], ['queued']*5)
        self.service.cancel(response['job'])
        run(job, work)
        self.assertEqual(job['status'], 'cancelled')
        cancelled = self.service.task_session_progress({'id':response['job']})
        self.assertEqual(cancelled['rows'][0]['status'], 'cancelled')
        self.assertFalse((self.root/'exports').exists())

    def test_batch_creates_one_folder_or_zip_with_separate_conversations(self):
        from zipfile import ZipFile
        self.rows = self.rows[:12]
        for row in self.rows:
            row['display'] = '同名会话'

        def export(archive, session, options, folder, assets, progress, cancel, **kwargs):
            path = folder/(session['username'] + '.txt')
            path.write_text(session['username'], encoding='utf-8')
            return dict(path=str(path), name=session['display'], count=1, format='txt')

        with patch.object(self.service, 'archive', return_value=self.archive), \
             patch('core.service.export_session', side_effect=export), patch.object(self.service.pool, 'submit') as submit:
            for zipped in (False, True):
                response = self.service.export({'sessions': [row['username'] for row in self.rows], 'format': 'txt', 'archiveZip': zipped})
                function, job, work = submit.call_args.args
                function(job, work)
                self.assertEqual(job['status'], 'completed', job['detail'])
                self.assertEqual(job['session_exported'], 12)
                path = self.service.task_file(response['job'])
                if zipped:
                    with ZipFile(path) as archive:
                        names = archive.namelist()
                        self.assertEqual(len({name.split('/')[0] for name in names}), 1)
                        self.assertEqual(len([name for name in names if name.endswith('/index.html')]), 1)
                        texts = {archive.read(name).decode('utf-8') for name in names if name.endswith('.txt')}
                else:
                    self.assertEqual(path.name, 'index.html')
                    self.assertEqual(len(list(path.parent.glob('*/'))), 12)
                    texts = {entry.read_text(encoding='utf-8') for entry in path.parent.glob('*/*.txt')}
                self.assertEqual(texts, {row['username'] for row in self.rows})

    def test_failed_batch_save_enqueues_nothing_and_rolls_back_in_memory(self):
        with patch.object(self.service, 'archive', return_value=self.archive), patch.object(self.service.pool, 'submit') as submit, patch.object(self.service, 'save', side_effect=OSError('Synthetic disk failure')):
            with self.assertRaises(OSError):
                self.service.export({'sessions': [row['username'] for row in self.rows]})
        submit.assert_not_called()
        self.assertEqual(self.service.config['tasks'], [])
        self.assertEqual(self.service.events, {})

    def test_batch_progress_is_weighted_by_scanned_rows(self):
        self.rows = self.rows[:2]
        short, long = (row['username'] for row in self.rows)
        self.archive.message_count = lambda session, *_: 1 if session == short else 10_000
        snapshots = []

        def scheduler(count, work, *args):
            self.assertEqual(count, 2)
            short_result = work(1)  # The longest conversation is scheduled first.
            self.assertEqual(short_result[0], 0)
            yield short_result
            snapshots.append(job['progress'])
            yield work(0)

        def export(archive, session, options, folder, assets, progress, cancel, **kwargs):
            if session['username'] == long:
                kwargs['scan_progress'](5000, 7)  # Most of the scanned rows are filtered out.
                snapshots.append(job['progress'])
                self.assertEqual(job['count'], 8)
                short_row, long_row = self.service.task_session_progress({'id':job['id']})['rows']
                self.assertEqual((short_row['status'], short_row['scanned']), ('completed', 1))
                self.assertEqual((long_row['status'],long_row['scanned'],long_row['total'],long_row['count']),
                                 ('running',5000,10_000,7))
            path = folder/'record.txt'
            path.write_text(session['username'], encoding='utf-8')
            return dict(path=str(path), name=session['display'], count=1 if session['username'] == short else 10, format='txt')

        with patch.object(self.service, 'archive', return_value=self.archive), \
             patch.object(self.service, '_run_export_batch', side_effect=scheduler), \
             patch('core.service.export_session', side_effect=export), \
             patch.object(self.service.pool, 'submit') as submit:
            self.service.export({'sessions': [short, long], 'format': 'txt'})
            run, job, work = submit.call_args.args
            run(job, work)
            self.assertEqual(job['status'], 'completed', job['detail'])
            self.assertEqual(job['progress'], 100)
        self.assertLess(snapshots[0], 10)
        self.assertTrue(45 <= snapshots[1] <= 49)

    def test_last_export_survives_reload_and_history_cleanup_is_scoped_to_account(self):
        self.service.library['account_dir'] = str(self.root/'account-a')
        original_library = dict(self.service.library)
        expected = [row['username'] for row in self.rows[:2]]

        def export(archive, session, options, folder, assets, progress, cancel, **kwargs):
            path = folder/'record.txt'
            path.write_text(session['username'], encoding='utf-8')
            return dict(path=str(path), name=session['display'], count=1, format='txt')

        with patch.object(self.service, 'archive', return_value=self.archive), \
             patch('core.service.export_session', side_effect=export), patch.object(self.service.pool, 'submit') as submit:
            self.service.export({'sessions': expected, 'format': 'txt'})
            run, job, work = submit.call_args.args
            run(job, work)
            self.assertEqual(job['status'], 'completed')
            self.assertTrue(self.service.state()['last_export_available'])
            self.service.delete_tasks({'clear': True})
            cancelled = self.service.export({'sessions': [self.rows[3]['username']], 'format': 'txt'})
            self.service.cancel(cancelled['job'])
            run, job, work = submit.call_args.args
            run(job, work)
            self.assertEqual(self.service.last_export_sessions(), expected)

        reloaded = Service(ASSETS, self.service.root)
        self.addCleanup(reloaded.shutdown)
        reloaded.library = {**original_library, 'root': str(self.root/'new-snapshot')}
        self.assertEqual(reloaded.last_export_sessions(), expected)
        reloaded.library = {**original_library, 'account_dir': str(self.root/'account-b')}
        self.assertEqual(reloaded.last_export_sessions(), [])
        self.assertFalse(reloaded.state()['last_export_available'])

    def test_batch_limits_concurrency_and_resumes_when_memory_is_available(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        memory = SimpleNamespace(total=8 * 1024**3, available=1024**3)
        started = [threading.Event() for _ in range(3)]
        release = threading.Event()
        def work(index):
            started[index].set()
            if not release.wait(5):
                raise TimeoutError('Synthetic export timed out')
            return index

        with patch('core.streaming.psutil.cpu_count', return_value=4), \
             patch('core.streaming.psutil.cpu_percent', return_value=10), \
             patch('core.streaming.psutil.virtual_memory', return_value=memory), ThreadPoolExecutor(1) as coordinator:
            done = coordinator.submit(lambda: list(
                self.service._run_export_batch(3, work, lambda: None, lambda: None)))
            try:
                self.assertTrue(started[0].wait(2))
                self.assertFalse(started[1].wait(0.55))
                memory.available = 4 * 1024**3
                self.assertTrue(started[1].wait(2))
                self.assertFalse(started[2].is_set())
            finally:
                release.set()
            self.assertEqual(sorted(done.result(timeout=5)), [0, 1, 2])
        self.assertTrue(all(event.is_set() for event in started))


class OfflinePrintingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.runtime = sync_playwright().start()
        cls.browser = cls.runtime.chromium.launch(channel='msedge', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runtime.stop()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)/'中文打印 # 100%'
        self.root.mkdir()
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.add_init_script('''window.print=()=>{
          const root=document.querySelector('#moect-print-root');
          window.__printed={rows:root.querySelectorAll('.row').length,posts:root.querySelectorAll('.post').length,
            text:root.textContent,display:getComputedStyle(root).display,
            expanded:[...root.querySelectorAll('details')].every(item=>item.open)};
        };''')

    def test_batch_directory_heading_aligns_with_the_conversation_cards(self):
        records = [
            dict(name='合成会话', path=self.root/'chat-1'/'index.html', count=520, format='html'),
            dict(name='很长的合成会话名称'*12, path=self.root/'chat-2'/'index.html', count=21, format='html'),
        ]
        export_index(self.root, records, directory=True, failures=['未导出的会话'])
        for width in (1440, 390):
            with self.subTest(width=width):
                self.page.set_viewport_size({'width': width, 'height': 900})
                self.page.goto((self.root/'index.html').as_uri())
                left_edges = self.page.locator('h1, body > p, h2, li').evaluate_all(
                    'items => items.map(item => item.getBoundingClientRect().left)')
                self.assertEqual(len(left_edges), 6)
                self.assertEqual(len(set(left_edges)), 1)
                self.assertGreaterEqual(left_edges[0], 16)
                self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))

    def test_chat_prints_all_matching_pages_and_restores_the_visible_page(self):
        archive = Archive(fixture(self.root/'数据库', count=170))
        self.addCleanup(archive.close)
        folder = self.root/'导出';folder.mkdir()
        result = export_session(archive, archive.sessions()[0], {'format': 'html', 'quickExport': True}, folder, ASSETS, lambda _: None, lambda: None)
        self.page.goto(Path(result['path']).as_uri())
        self.page.wait_for_function('() => !reading && total===170')
        self.page.locator('#search').fill('message 169')
        self.page.locator('#prev').click()
        self.page.wait_for_function('() => !reading && page===1 && total===170')
        visible = self.page.locator('#messages').text_content()
        self.page.emulate_media(media='print')
        self.page.locator('#print').click()
        self.page.wait_for_function('() => window.__printed && !MoectPrint.busy')
        result = self.page.evaluate('window.__printed')
        self.assertEqual((result['rows'], result['display']), (170, 'block'))
        self.assertEqual(self.page.locator('#messages').text_content(), visible)
        self.assertEqual(self.page.locator('#moect-print-root').count(), 0)
        self.page.emulate_media(media='screen')
        self.page.locator('#go').click()
        self.page.wait_for_function('() => !reading && total===1')
        self.page.locator('#print').click()
        self.page.wait_for_function('() => !MoectPrint.busy && window.__printed.rows===1')
        self.assertEqual(self.errors, [])

    def test_chat_keeps_the_current_page_when_an_earlier_chunk_is_incomplete(self):
        with closing(Archive(fixture(self.root/'数据库', count=580))) as archive:
            folder = self.root/'导出'
            folder.mkdir()
            result = export_session(archive, archive.sessions()[0], {'format':'html','quickExport':True},
                                    folder, ASSETS, lambda _: None, lambda: None)
        viewer = Path(result['path'])
        (viewer.parent/'c000001.js').write_text('window.__wxChunk=[];', encoding='utf-8')
        self.page.goto(viewer.as_uri())
        self.page.wait_for_function('() => !reading && page===7 && total===580')
        self.assertEqual(self.page.locator('#notice').text_content(), '')
        rows = self.page.locator('#messages').text_content()
        self.page.get_by_role('button', name='第 1 页', exact=True).click()
        self.page.wait_for_function('() => !reading && document.querySelector("#notice").textContent.includes("分片内容不完整")')
        self.assertEqual(self.page.locator('#page').text_content(), '第 8 / 8 页')
        self.assertEqual(self.page.locator('#messages').text_content(), rows)
        self.assertTrue(self.page.locator('#go').is_enabled())
        self.page.locator('#reset').click()
        self.page.wait_for_function('() => !reading && !document.querySelector("#notice").textContent')
        self.assertEqual(self.page.locator('#messages').text_content(), rows)
        self.assertEqual(self.errors, [])

    def test_chat_reference_jump_recovers_after_a_missing_target_and_highlights_the_original(self):
        with closing(Archive(fixture(self.root/'引用数据库', count=170))) as archive:
            messages = list(archive.messages(SESSION))
            messages[-3].update(kind='chatrecord', forward={'title':'窄窗口合并转发','messages':[]})
            messages[-2].update(kind='quote', text='定位测试（未收录）',
                                _quote={'server_id':'12345678','content':'归档外的引用正文','type':1})
            messages[-1].update(kind='quote', text='定位测试（已收录）', _quote={'server_id':'12'})
            out = self.root/'引用导出'
            out.mkdir()
            with patch.object(archive, 'messages', return_value=iter(messages)):
                result = export_session(archive, archive.sessions()[0], {'format':'html','quickExport':True},
                                        out, ASSETS, lambda _: None, lambda: None)
        self.page.goto(Path(result['path']).as_uri())
        self.page.wait_for_function('() => !reading && total===170')
        self.page.set_viewport_size({'width': 210, 'height': 900})
        self.page.wait_for_function('''() => document.documentElement.scrollWidth === innerWidth &&
            parseFloat(getComputedStyle(document.body).paddingBottom) >= document.querySelector('#archive-pagination').offsetHeight + 16''')
        self.page.set_viewport_size({'width': 1000, 'height': 800})
        self.page.locator('#search').fill('定位测试')
        self.page.locator('#go').click()
        self.page.wait_for_function('() => !reading && total===2')
        self.page.locator('#messages .row').filter(has_text='未收录').get_by_role('button', name='查看原消息').click()
        self.page.wait_for_function('() => !reading && document.querySelector("#notice").textContent.includes("原消息不在")')
        self.assertEqual(self.page.locator('#search').input_value(), '定位测试')
        self.assertEqual(self.page.locator('#messages .row').count(), 2)
        self.page.locator('#messages .row').filter(has_text='已收录').get_by_role('button', name='查看原消息').click()
        self.page.locator('#messages .row.focused[data-message-id="12"]').wait_for()
        self.assertEqual(self.page.locator('#search').input_value(), '')
        self.assertTrue(self.page.locator('#go').is_enabled())
        self.assertEqual(self.errors, [])

    def test_chat_preserves_text_links_and_opens_encoded_attachments_offline(self):
        picture = '图片 #100%.png'
        attachment = '附件 #100%.txt'
        with closing(Archive(fixture(self.root/'附件数据库', count=1))) as archive:
            message = next(archive.messages(SESSION))
            message.update(text='见（https://example.org/a_(b)?x=1&y=2）。[微笑] 还有 https://) 保留原文',
                           resources=[dict(kind='image', path=picture, status='available', role='main'),
                                      dict(kind='file', path=attachment, name='原始文件.txt', status='available')])
            out = self.root/'附件导出'
            out.mkdir()
            with patch.object(archive, 'messages', return_value=iter([message])), \
                 patch('core.exporting.ResourceStore.enrich', side_effect=lambda row: row):
                result = export_session(archive, archive.sessions()[0], {'format':'html'}, out,
                                        ASSETS, lambda _: None, lambda: None)
        viewer = Path(result['path'])
        (viewer.parent/picture).write_bytes(PNG)
        (viewer.parent/attachment).write_text('合成附件', encoding='utf-8')
        self.page.goto(viewer.as_uri())
        self.page.wait_for_function('() => !reading && total===1')
        href = self.page.locator('#messages a[href^="https:"]').get_attribute('href')
        self.assertEqual(href, 'https://example.org/a_(b)?x=1&y=2')
        self.assertIn('https://) 保留原文', self.page.locator('#messages').text_content())
        self.assertEqual(self.page.get_by_alt_text('[微笑]', exact=True).count(), 1)
        picture_link = self.page.locator('#messages a').filter(has=self.page.get_by_alt_text('图片', exact=True))
        picture_link.press('Enter')
        self.page.wait_for_function('document.querySelector(".moect-image-viewer[open] img")?.naturalWidth > 0')
        self.page.keyboard.press('Escape')
        local_file = self.page.get_by_role('link', name='打开附件：原始文件.txt')
        self.assertEqual(local_file.get_attribute('download'), '原始文件.txt')
        with self.page.expect_popup() as pending:
            local_file.click()
        pending.value.locator('pre').get_by_text('合成附件').wait_for()
        self.assertEqual(self.page.url, viewer.as_uri())
        self.assertEqual(self.errors, [])

    def test_chat_pagination_keeps_the_selected_colors_on_hover(self):
        with closing(Archive(fixture(self.root/'分页数据库', count=81))) as archive:
            for mode, accent in (('light', 'violet'), ('dark', 'blue')):
                with self.subTest(mode=mode, accent=accent):
                    folder = self.root/mode
                    folder.mkdir()
                    result = export_session(archive, archive.sessions()[0],
                                            {'format':'html','quickExport':True,'_theme':{'mode':mode,'accent':accent}},
                                            folder, ASSETS, lambda _: None, lambda: None)
                    self.page.goto(Path(result['path']).as_uri())
                    self.page.wait_for_function('() => !reading && total===81')
                    current = self.page.locator('#page-buttons button[aria-current="page"]')
                    other = self.page.locator('#page-buttons button:not([aria-current])')
                    colors = 'button=>{const style=getComputedStyle(button);return [style.backgroundColor,style.color,style.borderColor]}'
                    self.page.locator('h1').hover()
                    selected_colors = current.evaluate(colors)
                    current.hover()
                    self.assertEqual(current.evaluate(colors), selected_colors)
                    normal_colors = other.evaluate(colors)
                    other.hover()
                    hovered_colors = other.evaluate(colors)
                    self.assertNotEqual(hovered_colors[0], normal_colors[0])
                    self.assertEqual(hovered_colors[1:], [selected_colors[0]]*2)
                    other.click()
                    self.page.wait_for_function('() => !reading && page===0')
                    self.assertEqual(current.get_attribute('aria-label'), '第 1 页')

    def test_moments_link_is_content_width_and_print_honors_all_filtered_posts(self):
        (self.root/'image.png').write_bytes(PNG)
        comment_image = '评论 #100%.png'
        (self.root/comment_image).write_bytes(PNG)
        data = dict(category='moments', name='Synthetic Moments', owner={'name': 'Synthetic owner'},
                    notice='Local synthetic data', resource_summary={}, exported_at='2026-11-06',
                    items=[dict(id=str(index), author='friend', author_name='朋友', text='合成动态 ' + str(index),
                                title='微信小视频', url='https://example.com/video', create_time=1800000000 + index,
                                resources=[dict(kind='image', status='available', path='image.png')],
                                likes=[], comments=[]) for index in range(65)])
        data['items'][0]['comments'] = [dict(name='评论者', text='带图评论',
            resources=[dict(kind='image', status='available', path=comment_image)])]
        write_collection(self.root, ASSETS, data)
        self.page.goto((self.root/'index.html').as_uri())
        self.page.wait_for_function("() => document.querySelectorAll('.post').length===60")
        self.page.locator('a').filter(has=self.page.get_by_alt_text('评论图片或表情')).press('Enter')
        self.page.wait_for_function('document.querySelector(".moect-image-viewer[open] img")?.naturalWidth > 0')
        self.page.keyboard.press('Escape')
        width = self.page.locator('.post .link').first.evaluate('element=>element.getBoundingClientRect().width')
        self.assertLess(width, 180)
        self.page.locator('#print').click()
        self.page.wait_for_function('() => window.__printed && !MoectPrint.busy')
        self.assertEqual(self.page.evaluate('window.__printed.posts'), 65)
        self.assertEqual(self.page.locator('#items .post').count(), 60)
        self.page.locator('#query').fill('合成动态 64')
        self.page.locator('#print').click()
        self.page.wait_for_function('() => !MoectPrint.busy && window.__printed.posts===1')
        self.assertEqual(self.errors, [])

    def test_asset_archives_share_chat_layout_and_show_invalid_jumps_with_header_collapsed(self):
        for category, page_size, mode in (('moments', 60, 'dark'), ('favorites', 40, 'light')):
            with self.subTest(category=category):
                folder = self.root/category
                folder.mkdir()
                data = dict(name='离线样例', category=category, owner={'name': '示例用户'},
                            notice='合成归档', exported_at='2026-11-25T19:00:00',
                            theme={'mode': mode, 'accent': 'cyan'},
                            items=[dict(id=str(n), author='friend', author_name='朋友', source='朋友',
                                        type=1, type_name='文本', title=f'条目 {n}', text=f'样例 {n}',
                                        create_time=1800000000+n)
                                   for n in range(20*page_size)])
                if category == 'moments':
                    write_collection(folder, ASSETS, data)
                else:
                    template = archive_viewer_template(ASSETS, 'favorites.html', data['theme'])
                    (folder/'index.html').write_text(template.replace('__FAVORITES__', json.dumps(data)), encoding='utf-8')
                    (folder/'builtin-faces.js').write_text('', encoding='utf-8')
                self.page.set_viewport_size({'width': 1200, 'height': 800})
                self.page.goto((folder/'index.html').as_uri())
                header, footer = self.page.locator('body > header'), self.page.locator('#archive-pagination')
                self.assertEqual(header.evaluate('node => getComputedStyle(node).position'), 'sticky')
                self.assertEqual(footer.evaluate('node => getComputedStyle(node).position'), 'fixed')
                for bar in (header, footer):
                    self.assertIn('blur(8px)', bar.evaluate('node => getComputedStyle(node).backdropFilter'))
                title_x = self.page.locator('#title').bounding_box()['x']
                entry_x = self.page.locator('#items article').first.bounding_box()['x']
                self.assertAlmostEqual(title_x, entry_x, delta=1)
                self.page.locator('#toggle-header').click()
                self.assertEqual(self.page.locator('#toggle-header').get_attribute('aria-expanded'), 'false')
                jump = self.page.locator('#page-jump')
                jump.fill('21')
                jump.press('Enter')
                footer.get_by_text('请输入有效页码', exact=True).wait_for(timeout=2000)
                self.assertEqual(jump.get_attribute('aria-invalid'), 'true')
                self.assertEqual(footer.locator('#page').inner_text(), '第 1 / 20 页')
                jump.fill('20')
                jump.press('Enter')
                self.assertEqual(footer.locator('#page').inner_text(), '第 20 / 20 页')
                self.assertEqual(jump.get_attribute('aria-invalid'), None)
                self.page.set_viewport_size({'width': 390, 'height': 800})
                self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                self.page.locator('#toggle-header').click()
                self.page.locator('#query').fill(f'样例 {20*page_size-1}')
                self.assertEqual(footer.locator('#page').inner_text(), '第 1 / 1 页')
                self.assertTrue(footer.locator('#page-jump-controls').is_hidden())
                self.assertEqual(self.errors, [])
