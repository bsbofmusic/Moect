"""Python adaptation of QCE's bounded buffered writer and batch source design.

Based on shuakami/qq-chat-exporter, qq-chat-export-core/src/stream_utils.rs
and text_exporter.rs (GPL-3.0). See THIRD_PARTY_NOTICES.md.
"""
import json
import os
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import psutil

DEFAULT_FLUSH_THRESHOLD = 1024 * 1024
JSON_LINE_SEPARATORS = {0x85: '\\u0085', 0x2028: '\\u2028', 0x2029: '\\u2029'}


def json_line(record):
    return json.dumps(record, ensure_ascii=False).translate(JSON_LINE_SEPARATORS) + '\n'


def export_worker_limit():
    cores = psutil.cpu_count(logical=False) or os.cpu_count() or 1
    return 1 if cores <= 1 else min(4, max(2, cores // 2))


def export_capacity():
    cpu = psutil.cpu_percent(interval=0.15)
    measured = time.monotonic()

    def available(active):
        nonlocal cpu, measured
        now = time.monotonic()
        if now - measured >= 0.4:
            cpu = psutil.cpu_percent()
            measured = now
        memory = psutil.virtual_memory()
        reserve = min(768 * 1024**2, memory.total // 8)
        return memory.available >= reserve + active * 512 * 1024**2 and (not active or cpu < 85)

    return available


def run_export_workers(items, work, check, on_wait, *, ordered=False, limit=None, capacity=None):
    """Bound queued work and completed results, including in output order."""
    limit = limit or export_worker_limit()
    active = deque()
    items = iter(items)
    end = object()
    item = next(items, end)
    if item is end:
        return
    capacity = capacity or export_capacity()
    with ThreadPoolExecutor(max_workers=limit, thread_name_prefix='export') as pool:
        try:
            while item is not end or active:
                check()
                ready = ([active[0]] if active and active[0].done() else []) if ordered else [f for f in active if f.done()]
                for future in ready:
                    active.remove(future)
                    yield future.result()
                if item is not end and len(active) < limit:
                    if capacity(len(active)):
                        active.append(pool.submit(work, item))
                        item = next(items, end)
                        continue
                    on_wait()
                if active:
                    wait([active[0]] if ordered else active, timeout=0.4, return_when=FIRST_COMPLETED)
                elif item is not end:
                    time.sleep(0.4)
        finally:
            for future in active:
                future.cancel()


def write_archive_data(folder, template, marker, data, cancel):
    """Write each collection record once to both the JSON and HTML archives."""
    before, found, after = template.partition(marker)
    if not found:
        raise ValueError('归档模板缺少数据位置。')
    escape = {ord('<'): '\\u003c', 0x2028: '\\u2028', 0x2029: '\\u2029'}
    with (folder/'data.json').open('w', encoding='utf-8') as raw, (folder/'index.html').open('w', encoding='utf-8') as html:
        html.write(before)

        def write(part):
            raw.write(part)
            html.write(part.translate(escape) if any(c in part for c in '<\u2028\u2029') else part)

        write('{')
        for number, (key, value) in enumerate(data.items()):
            cancel()
            write((',\n  ' if number else '\n  ') + json.dumps(key) + ': ')
            if key == 'items':
                write('[')
                for index, item in enumerate(value):
                    cancel()
                    write((',\n    ' if index else '\n    ') + json.dumps(item, ensure_ascii=False))
                write('\n  ]' if value else ']')
            else:
                write(json.dumps(value, ensure_ascii=False))
        write('\n}')
        html.write(after)


class BufferedTextWriter:
    def __init__(self, path, threshold=DEFAULT_FLUSH_THRESHOLD, encoding='utf-8'):
        self.path = Path(path)
        self.file = open(self.path, 'w', encoding=encoding, newline='')
        self.parts = []
        self.size = 0
        self.threshold = threshold
        self.bytes_written = 0

    def write(self, text):
        if not text:
            return
        self.parts.append(text)
        self.size += len(text.encode('utf-8'))
        if self.size >= self.threshold:
            self.flush()

    def flush(self):
        if self.parts:
            self.file.write(''.join(self.parts))
            self.bytes_written += self.size
            self.parts.clear()
            self.size = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if not exc_type:
                self.flush()
                self.file.flush()
                os.fsync(self.file.fileno())
        finally:
            self.file.close()

