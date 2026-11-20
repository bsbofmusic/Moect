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
from core.exporting import export_session
from core.service import Service
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

        with patch('core.service.psutil.cpu_count', return_value=4), \
             patch('core.service.psutil.cpu_percent', return_value=10), \
             patch('core.service.psutil.virtual_memory', return_value=memory), ThreadPoolExecutor(1) as coordinator:
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

    def test_chat_prints_all_matching_pages_and_restores_the_visible_page(self):
        archive = Archive(fixture(self.root/'数据库', count=170))
        self.addCleanup(archive.close)
        folder = self.root/'导出';folder.mkdir()
        result = export_session(archive, archive.sessions()[0], {'format': 'html', 'quickExport': True}, folder, ASSETS, lambda _: None, lambda: None)
        self.page.goto(Path(result['path']).as_uri())
        self.page.wait_for_function('() => !reading && total===170')
        visible = self.page.locator('#messages').text_content()
        self.page.emulate_media(media='print')
        self.page.locator('#print').click()
        self.page.wait_for_function('() => window.__printed && !MoectPrint.busy')
        result = self.page.evaluate('window.__printed')
        self.assertEqual((result['rows'], result['display']), (170, 'block'))
        self.assertEqual(self.page.locator('#messages').text_content(), visible)
        self.assertEqual(self.page.locator('#moect-print-root').count(), 0)
        self.page.emulate_media(media='screen')
        self.page.locator('#search').fill('message 169')
        self.page.locator('#go').click()
        self.page.wait_for_function('() => !reading && total===1')
        self.page.locator('#print').click()
        self.page.wait_for_function('() => !MoectPrint.busy && window.__printed.rows===1')
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
        data = dict(category='moments', name='Synthetic Moments', owner={'name': 'Synthetic owner'},
                    notice='Local synthetic data', resource_summary={}, exported_at='2026-11-06',
                    items=[dict(id=str(index), author='friend', author_name='朋友', text='合成动态 ' + str(index),
                                title='微信小视频', url='https://example.com/video', create_time=1800000000 + index,
                                resources=[dict(kind='image', status='available', path='image.png')],
                                likes=[], comments=[]) for index in range(65)])
        write_collection(self.root, ASSETS, data)
        self.page.goto((self.root/'index.html').as_uri())
        self.page.wait_for_function("() => document.querySelectorAll('.post').length===60")
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
