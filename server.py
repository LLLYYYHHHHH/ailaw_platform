import json
import argparse
import csv
import io
import re
import os
import queue
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
from urllib.parse import urlsplit
from providers import ProviderError, settings, public_models, stream_model

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'experiments.sqlite3'
RUN_SLOTS = threading.BoundedSemaphore(2)

@contextmanager
def connect():
    db = sqlite3.connect(DB, timeout=15)
    try:
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS experiments (id INTEGER PRIMARY KEY, created TEXT, payload TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS reviews (id INTEGER PRIMARY KEY, experiment_id INTEGER NOT NULL, created TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS reviews_experiment ON reviews(experiment_id)')
            yield db
    finally:
        db.close()

def save(experiment_id, payload):
    with connect() as db:
        db.execute('UPDATE experiments SET payload=? WHERE id=?', (json.dumps(payload, ensure_ascii=False), experiment_id))

def hydrate(row, db):
    reviews = db.execute('SELECT id, created, payload FROM reviews WHERE experiment_id=? ORDER BY id DESC', (row[0],)).fetchall()
    return {**json.loads(row[2]), 'id': row[0], 'created': row[1], 'reviews': [{**json.loads(r[2]), 'id': r[0], 'created': r[1]} for r in reviews]}

def csv_export(experiment):
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['实验编号','实验时间','模式','问题','系统提示词','模型名称','请求模型','返回模型','状态','耗时毫秒','首字时延毫秒','Token用量','原始回答','错误','评价人','评价时间','评分','评价备注','片段标记'])
    def safe(value):
        value = '' if value is None else str(value)
        return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value
    for answer in experiment['answers']:
        reviews = [r for r in experiment.get('reviews', []) if r['model'] == answer['model']] or [{}]
        for review in reviews:
            row = [experiment['id'],experiment['created'],experiment['mode'],experiment['question'],experiment['system_prompt'],answer.get('name',answer['model']),answer.get('requested_model'),answer.get('returned_model'),answer['status'],answer.get('elapsed_ms'),answer.get('first_token_ms'),(answer.get('usage') or {}).get('total_tokens'),answer['text'],answer.get('error'),review.get('reviewer'),review.get('created'),json.dumps(review.get('scores',[]),ensure_ascii=False),review.get('notes'),json.dumps(review.get('annotations',[]),ensure_ascii=False)]
            writer.writerow([safe(v) for v in row])
    return ('\ufeff' + output.getvalue()).encode('utf-8')

def validate_review(data, payload):
    model = data.get('model')
    answer = next((a for a in payload['answers'] if a['model'] == model), None)
    if not answer:
        raise ValueError('评价模型不属于本次实验')
    if payload.get('status') == 'running' or answer['status'] in ('queued','running'):
        raise ValueError('请等待实验结束后再评价')
    reviewer = data.get('reviewer', '')
    notes = data.get('notes', '')
    scores = data.get('scores', [])
    annotations = data.get('annotations', [])
    if not isinstance(reviewer,str) or not reviewer.strip() or len(reviewer) > 100:
        raise ValueError('请填写评价人，最多 100 字符')
    if not isinstance(notes,str) or len(notes) > 5000:
        raise ValueError('评价备注最多 5000 字符')
    if not isinstance(scores,list) or len(scores) > 10:
        raise ValueError('评分项最多 10 个')
    clean_scores = []
    dimensions = set()
    for score in scores:
        if not isinstance(score,dict):
            raise ValueError('评分格式不正确')
        dimension,value = score.get('dimension'),score.get('value')
        if not isinstance(dimension,str) or not dimension.strip() or len(dimension)>40 or dimension.strip() in dimensions:
            raise ValueError('评分项名称需唯一，最多 40 字符')
        if type(value) is not int or not 1 <= value <= 5:
            raise ValueError('评分应为 1–5 的整数')
        dimensions.add(dimension.strip())
        clean_scores.append({'dimension':dimension.strip(),'value':value})
    if not isinstance(annotations,list) or len(annotations)>50:
        raise ValueError('片段标记最多 50 条')
    clean_annotations=[]
    for mark in annotations:
        if not isinstance(mark,dict):
            raise ValueError('片段标记格式不正确')
        start,end = mark.get('start'),mark.get('end')
        quote,label,reason = mark.get('quote'),mark.get('label'),mark.get('reason','')
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(answer['text']) or answer['text'][start:end] != quote:
            raise ValueError('标记片段与原始回答不匹配，请重新选取')
        if not isinstance(label,str) or not label.strip() or len(label)>40 or not isinstance(reason,str) or not reason.strip() or len(reason)>2000:
            raise ValueError('请填写标记类型和理由，理由最多 2000 字符')
        clean_annotations.append({'start':start,'end':end,'quote':quote,'label':label.strip(),'reason':reason.strip(),'status':'pending'})
    if not clean_scores and not notes.strip() and not clean_annotations:
        raise ValueError('至少填写一项评分、备注或片段标记')
    return {'model':model,'reviewer':reviewer.strip(),'scores':clean_scores,'notes':notes.strip(),'annotations':clean_annotations}

def recover_interrupted():
    with connect() as db:
        for experiment_id, raw in db.execute('SELECT id, payload FROM experiments').fetchall():
            payload = json.loads(raw)
            if payload.get('status') == 'running':
                payload['status'] = 'interrupted'
                for answer in payload['answers']:
                    if answer['status'] in ('queued', 'running'):
                        answer.update(status='failed', error='服务重启中断了本次调用')
                db.execute('UPDATE experiments SET payload=? WHERE id=?', (json.dumps(payload, ensure_ascii=False), experiment_id))

class Handler(BaseHTTPRequestHandler):
    def send_json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/api/models':
            try:
                return self.send_json(public_models(settings()))
            except ProviderError as exc:
                return self.send_json({'error': str(exc)}, 500)
        if path == '/api/experiments':
            with connect() as db:
                rows = db.execute('SELECT id, created, payload FROM experiments ORDER BY id DESC LIMIT 100').fetchall()
                experiments = [hydrate(r, db) for r in rows]
            return self.send_json(experiments)
        detail = re.fullmatch(r'/api/experiments/(\d+)(/export\.(?:csv|json))?', path)
        if detail:
            with connect() as db:
                row = db.execute('SELECT id, created, payload FROM experiments WHERE id=?', (int(detail[1]),)).fetchone()
                experiment = hydrate(row, db) if row else None
            if not experiment:
                return self.send_json({'error':'实验不存在'},404)
            if detail[2]:
                extension = 'csv' if detail[2].endswith('.csv') else 'json'
                body = csv_export(experiment) if extension == 'csv' else json.dumps(experiment,ensure_ascii=False,indent=2).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type','text/csv; charset=utf-8' if extension == 'csv' else 'application/json; charset=utf-8')
                self.send_header('Content-Disposition',f'attachment; filename="experiment-{detail[1]}.{extension}"')
                self.send_header('Content-Length',str(len(body)))
                self.send_header('Cache-Control','no-store')
                self.end_headers()
                return self.wfile.write(body)
            return self.send_json(experiment)
        if path in ('/app.js','/styles.css'):
            body = (ROOT / path[1:]).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/javascript; charset=utf-8' if path.endswith('.js') else 'text/css; charset=utf-8')
            self.send_header('Content-Length',str(len(body)))
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            return self.wfile.write(body)
        if path != '/':
            return self.send_json({'error': 'Not found'}, 404)
        body = (ROOT / 'index.html').read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        review_path = re.fullmatch(r'/api/experiments/(\d+)/reviews',self.path)
        if self.path not in ('/api/experiments', '/api/experiments/stream') and not review_path:
            return self.send_json({'error': 'Not found'}, 404)
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + self.headers.get('Host', ''):
            return self.send_json({'error': '请求来源不匹配'}, 403)
        if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
            return self.send_json({'error': '请使用 JSON 请求'}, 415)
        try:
            size = int(self.headers.get('Content-Length', 0))
            if not 0 < size <= 100000:
                raise ValueError('输入大小不合法')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError('请求格式不正确')
            if review_path:
                return self.save_review(int(review_path[1]), data)
            question = data.get('question', '')
            prompt = data.get('system_prompt', '')
            ids = data.get('models', [])
            mode = data.get('mode', 'mock')
            if not isinstance(question, str) or not question.strip() or len(question) > 20000:
                raise ValueError('请输入问题，最多 20000 字符')
            if not isinstance(prompt, str) or len(prompt) > 10000:
                raise ValueError('系统提示词最多 10000 字符')
            if not isinstance(ids, list) or not 1 <= len(ids) <= 5 or any(not isinstance(i, str) for i in ids):
                raise ValueError('请选择 1–5 个模型')
            ids = list(dict.fromkeys(ids))
            if mode not in ('mock', 'real'):
                raise ValueError('模式不正确')
            configs = settings() if mode == 'real' else {}
            if mode == 'real':
                if any(i not in configs or not configs[i]['configured'] for i in ids):
                    raise ValueError('所选模型尚未配置密钥，请配置后刷新模型列表')
            elif any(i not in ('A', 'B', 'C') for i in ids):
                raise ValueError('模拟模型不存在')
        except ProviderError as exc:
            return self.send_json({'error': str(exc)}, 500)
        except (ValueError, TypeError, UnicodeError) as exc:
            message = '请求格式不正确' if isinstance(exc, json.JSONDecodeError) else str(exc)
            return self.send_json({'error': message}, 400)

        if not RUN_SLOTS.acquire(blocking=False):
            return self.send_json({'error': '已有两组实验运行中，请稍后再试'}, 429)
        try:
            created = datetime.now(timezone.utc).isoformat()
            payload = {'question': question, 'system_prompt': prompt, 'mode': mode, 'status': 'running', 'answers': []}
            for model_id in ids:
                config = configs.get(model_id, {})
                payload['answers'].append({'model': model_id, 'name': config.get('name', '模拟模型 ' + model_id), 'requested_model': config.get('model'), 'text': '', 'status': 'queued', 'usage': None, 'first_token_ms': None, 'elapsed_ms': None})
            with connect() as db:
                cursor = db.execute('INSERT INTO experiments(created,payload) VALUES (?,?)', (created, json.dumps(payload, ensure_ascii=False)))
                experiment_id = cursor.lastrowid
            events = queue.Queue()
            events.put({'type': 'created', 'experiment': json.loads(json.dumps(dict(id=experiment_id, created=created, reviews=[], **payload)))})
            lock = threading.Lock()

            def worker(answer):
                def changed():
                    with lock:
                        save(experiment_id, payload)
                        snapshot = json.loads(json.dumps(answer, ensure_ascii=False))
                    events.put({'type': 'answer', 'answer': snapshot})
                if mode == 'mock':
                    answer.update(status='completed', text=f'【模拟回答，非真实模型输出】\n\n问题：{question}\n\n这是模型 {answer["model"]} 的演示内容。', elapsed_ms=0)
                    changed()
                else:
                    messages = ([{'role': 'system', 'content': prompt}] if prompt else []) + [{'role': 'user', 'content': question}]
                    stream_model(configs[answer['model']], messages, answer, changed)

            disconnected = False
            streaming = self.path.endswith('/stream')
            if streaming:
                self.send_response(200)
                self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Accel-Buffering', 'no')
                self.end_headers()
            with ThreadPoolExecutor(max_workers=len(ids)) as pool:
                futures = [pool.submit(worker, a) for a in payload['answers']]
                while not all(f.done() for f in futures) or not events.empty():
                    try:
                        event = events.get(timeout=1)
                    except queue.Empty:
                        event = {'type': 'heartbeat'}
                    if streaming and not disconnected:
                        try:
                            self.wfile.write((json.dumps(event, ensure_ascii=False) + '\n').encode())
                            self.wfile.flush()
                        except OSError:
                            disconnected = True
                for future, answer in zip(futures, payload['answers']):
                    if future.exception():
                        answer.update(status='failed', error='实验执行异常')
            payload['status'] = 'completed' if all(a['status'] == 'completed' for a in payload['answers']) else 'failed'
            save(experiment_id, payload)
            final = dict(id=experiment_id, created=created, reviews=[], **payload)
            if streaming and not disconnected:
                try:
                    self.wfile.write((json.dumps({'type': 'done', 'experiment': final}, ensure_ascii=False) + '\n').encode())
                    self.wfile.flush()
                except OSError:
                    pass
            elif not streaming:
                self.send_json(final, 201)
        finally:
            RUN_SLOTS.release()

    def save_review(self, experiment_id, data):
        with connect() as db:
            row = db.execute('SELECT payload FROM experiments WHERE id=?',(experiment_id,)).fetchone()
            if not row:
                return self.send_json({'error':'实验不存在'},404)
            review = validate_review(data,json.loads(row[0]))
            created = datetime.now(timezone.utc).isoformat()
            cursor = db.execute('INSERT INTO reviews(experiment_id,created,payload) VALUES (?,?,?)',(experiment_id,created,json.dumps(review,ensure_ascii=False)))
            review_id = cursor.lastrowid
        return self.send_json({**review,'id':review_id,'created':created},201)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8000')))
    args = parser.parse_args()
    recover_interrupted()
    port = args.port
    print(f'平台已启动：http://127.0.0.1:{port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
