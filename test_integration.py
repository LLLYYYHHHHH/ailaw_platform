import io
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib import request, error

import providers
import server

def config(model_id='test'):
    return dict(id=model_id, name=model_id, model='fixture-model', base_url='https://fixture.example/v1', api_key_env='TEST_KEY', _key='secret-fixture-token', parameters={'max_tokens': 50}, configured=True)

class FakeStream(io.BytesIO):
    headers = {'Content-Type': 'text/event-stream'}

def sse(*chunks):
    return ''.join('data: ' + (c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)) + '\n\n' for c in chunks).encode()

class AdapterTests(unittest.TestCase):
    def test_disabled_provider_is_not_available(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'models.json').write_text(json.dumps([{'id':'paused','enabled':False}]),encoding='utf-8')
            with patch.object(providers,'ROOT',root),patch.object(providers,'CONFIG_PATH',root/'models.json'):
                self.assertEqual(providers.settings(),{})

    def test_local_key_overrides_inherited_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'.env').write_text('TEST_KEY=local-test-key\n',encoding='utf-8')
            cfg=config()
            for field in ('_key','configured'):
                cfg.pop(field)
            (root/'models.json').write_text(json.dumps([cfg]),encoding='utf-8')
            with patch.object(providers,'ROOT',root),patch.object(providers,'CONFIG_PATH',root/'models.json'),patch.dict('os.environ',{'TEST_KEY':'inherited-test-key'}):
                self.assertEqual(providers.settings()['test']['_key'],'local-test-key')

    def call(self, data=None, exc=None):
        answer = dict(text='', first_token_ms=None)
        updates = []
        with patch('providers.request.build_opener') as opener:
            opener.return_value.open.return_value = FakeStream(data or b'')
            if exc:
                opener.return_value.open.side_effect = exc
            providers.stream_model(config(), [{'role': 'user', 'content': '你好'}], answer, lambda: updates.append(dict(answer)))
            req = opener.return_value.open.call_args.args[0]
            self.assertEqual(req.get_header('Authorization'), 'Bearer secret-fixture-token')
            self.assertEqual(json.loads(req.data)['messages'][0]['content'], '你好')
        self.assertNotIn('secret-fixture-token', json.dumps(answer))
        return answer, updates

    def test_stream_usage_and_finish(self):
        answer, updates = self.call(sse({'model':'snapshot-1','id':'req-1','choices':[{'delta':{'content':'你'}}]}, {'choices':[{'delta':{'content':'好'},'finish_reason':'length'}]}, {'choices':[],'usage':{'total_tokens':5}}, '[DONE]'))
        self.assertEqual(answer['text'], '你好')
        self.assertEqual(answer['status'], 'completed')
        self.assertEqual(answer['returned_model'], 'snapshot-1')
        self.assertEqual(answer['usage']['total_tokens'], 5)
        self.assertEqual(answer['finish_reason'], 'length')
        self.assertIsNotNone(answer['first_token_ms'])
        self.assertTrue(any(a['text'] == '你' for a in updates))

    def test_partial_disconnect(self):
        answer, _ = self.call(sse({'choices':[{'delta':{'content':'部分回答'}}]}))
        self.assertEqual(answer['status'], 'failed')
        self.assertEqual(answer['text'], '部分回答')

    def test_responses_pro_request_and_usage(self):
        answer = dict(text='', first_token_ms=None)
        cfg = {**config(), 'api':'responses','model':'gpt-5.5-pro', 'parameters':{'max_completion_tokens':4096}}
        response = {'id':'resp-test','model':'gpt-5.5-pro','status':'completed','output':[{'type':'reasoning'}, {'type':'message','content':[{'type':'output_text','text':'连接成功'}]}],'usage':{'input_tokens':10,'output_tokens':20,'total_tokens':30}}
        with patch('providers.request.build_opener') as opener:
            opener.return_value.open.return_value=io.BytesIO(json.dumps(response).encode())
            providers.stream_model(cfg,[{'role':'user','content':'test'}],answer,lambda:None)
            req=opener.return_value.open.call_args.args[0]
            body=json.loads(req.data)
        self.assertTrue(req.full_url.endswith('/responses'))
        self.assertFalse(body['stream'])
        self.assertFalse(body['store'])
        self.assertEqual(body['max_output_tokens'],4096)
        self.assertNotIn('max_completion_tokens',body)
        self.assertEqual(answer['text'],'连接成功')
        self.assertEqual(answer['usage']['total_tokens'],30)
        self.assertIsNone(answer['first_token_ms'])
        self.assertEqual(answer['status'],'completed')

    def test_responses_incomplete_preserves_text(self):
        answer=dict(text='',first_token_ms=None)
        response={'status':'incomplete','incomplete_details':{'reason':'max_output_tokens'},'output':[{'type':'message','content':[{'type':'output_text','text':'部分回答'}]}]}
        with patch('providers.request.build_opener') as opener:
            opener.return_value.open.return_value=io.BytesIO(json.dumps(response).encode())
            providers.stream_model({**config(),'api':'responses'},[],answer,lambda:None)
        self.assertEqual(answer['status'],'failed')
        self.assertEqual(answer['text'],'部分回答')

    def test_http_error_is_sanitized(self):
        exc = error.HTTPError('https://fixture.example', 401, 'secret-fixture-token', {}, io.BytesIO(b'secret-fixture-token'))
        answer, _ = self.call(exc=exc)
        self.assertEqual(answer['status'], 'failed')
        self.assertIn('401', answer['error'])

    def test_quota_error_code_without_upstream_message(self):
        body={'error':{'code':'provider_specific_code','type':'insufficient_quota','message':'secret-fixture-token'}}
        exc=error.HTTPError('https://fixture.example',429,'Too many requests',{},io.BytesIO(json.dumps(body).encode()))
        answer,_=self.call(exc=exc)
        self.assertEqual(answer['error_code'],'insufficient_quota')
        self.assertEqual(answer['http_status'],429)

class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(server, 'DB', Path(self.tmp.name) / 'test.sqlite3'))
        self.stack.enter_context(patch.object(server, 'settings', return_value={'good':config('good'), 'bad':config('bad')}))
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.url = 'http://127.0.0.1:' + str(self.http.server_port)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.stack.close()
        self.tmp.cleanup()

    def post(self, data, path='/api/experiments/stream'):
        return request.urlopen(request.Request(self.url+path, data=json.dumps(data).encode(), headers={'Content-Type':'application/json'}), timeout=5)

    def test_parallel_failure_isolation_and_storage(self):
        barrier = threading.Barrier(2)
        def fake(config, messages, answer, changed):
            self.assertEqual(messages[0], {'role':'system','content':'提示'})
            barrier.wait(timeout=3)  # Fails if models run sequentially.
            answer.update(status='running', text='片段')
            changed()
            if config['id']=='good':
                answer.update(status='completed',text='完整回答',usage={'total_tokens':7})
            else:
                answer.update(status='failed',error='fixture failure')
            changed()
        with patch.object(server, 'stream_model', side_effect=fake):
            with self.post({'mode':'real','question':'问题','system_prompt':'提示','models':['good','bad']}) as response:
                events = [json.loads(line) for line in response if line.strip()]
        self.assertEqual(events[0]['type'], 'created')
        final=events[-1]['experiment']
        self.assertEqual([a['status'] for a in final['answers']], ['completed','failed'])
        self.assertTrue(any(e.get('answer',{}).get('text')=='片段' for e in events))
        with request.urlopen(self.url+'/api/experiments') as response:
            saved=json.load(response)
        self.assertEqual(saved[0],final)
        self.assertNotIn('secret-fixture-token',json.dumps(saved))

    def test_model_metadata_and_files_do_not_expose_secrets(self):
        with request.urlopen(self.url+'/api/models') as response:
            public=json.load(response)
        self.assertNotIn('secret-fixture-token',json.dumps(public))
        self.assertNotIn('_key',json.dumps(public))
        for path in ('/.env','/models.json','/experiments.sqlite3'):
            with self.assertRaises(error.HTTPError) as caught:
                request.urlopen(self.url+path)
            self.assertEqual(caught.exception.code,404)

    def test_missing_config_and_bad_input(self):
        for data in ({'mode':'real','question':'x','models':['unknown']}, {'question':[], 'models':['A']}, {'question':'x','models':'A'}):
            with self.assertRaises(error.HTTPError) as caught:
                self.post(data)
            self.assertEqual(caught.exception.code,400)

    def test_mock_compatibility_and_restart_recovery(self):
        with self.post({'question':'演示','models':['A']}, '/api/experiments') as response:
            experiment=json.load(response)
        self.assertEqual(experiment['mode'],'mock')
        experiment.pop('id');experiment.pop('created')
        experiment['status']='running';experiment['answers'][0]['status']='running'
        server.save(1,experiment)
        server.recover_interrupted()
        with request.urlopen(self.url+'/api/experiments') as response:
            recovered=json.load(response)[0]
        self.assertEqual(recovered['status'],'interrupted')
        self.assertEqual(recovered['answers'][0]['status'],'failed')

    def mock_experiment(self):
        with self.post({'question':'你好😀','models':['A','B']},'/api/experiments') as r:
            return json.load(r)

    def test_review_anchor_persists_without_mutating_answers(self):
        original=self.mock_experiment()
        text=original['answers'][0]['text']
        start=text.index('😀')
        data={'model':'A','reviewer':'测试员','scores':[{'dimension':'清晰度','value':4}],'notes':'记录依据','annotations':[{'start':start,'end':start+1,'quote':'😀','label':'其他','reason':'用于验证字符定位','status':'confirmed'}]}
        with self.post(data,f'/api/experiments/{original["id"]}/reviews') as r:
            review=json.load(r)
        self.assertEqual(review['annotations'][0]['status'],'pending')
        with request.urlopen(self.url+f'/api/experiments/{original["id"]}') as r:
            detail=json.load(r)
        self.assertEqual(detail['answers'],original['answers'])
        self.assertEqual(detail['reviews'],[review])
        # Re-saving generation must not overwrite separately stored evaluation.
        server.save(original['id'],{k:v for k,v in original.items() if k not in ('id','created','reviews')})
        with request.urlopen(self.url+'/api/experiments') as r:
            history=json.load(r)
        self.assertEqual(history[0]['reviews'],[review])

    def test_invalid_review_rejected(self):
        e=self.mock_experiment()
        base={'model':'A','reviewer':'测试员','scores':[{'dimension':'测试','value':3}]}
        invalid=[{**base,'model':'foreign'}, {**base,'scores':[{'dimension':'测试','value':True}]}, {**base,'scores':[{'dimension':'测试','value':6}]}, {**base,'annotations':[{'start':0,'end':2,'quote':'伪造','label':'错误','reason':'x'}]}, {**base,'reviewer':' '}]
        for data in invalid:
            with self.assertRaises(error.HTTPError) as caught:
                self.post(data,f'/api/experiments/{e["id"]}/reviews')
            self.assertEqual(caught.exception.code,400)
        with self.assertRaises(error.HTTPError) as caught:
            self.post(base,'/api/experiments/9999/reviews')
        self.assertEqual(caught.exception.code,404)

    def test_csv_includes_review_and_neutralizes_formulas(self):
        import csv
        e=self.mock_experiment()
        with self.post({'model':'A','reviewer':'测试员','notes':'=SUM(1,2)'},f'/api/experiments/{e["id"]}/reviews') as r:
            json.load(r)
        with request.urlopen(self.url+f'/api/experiments/{e["id"]}/export.csv') as r:
            rows=list(csv.DictReader(io.StringIO(r.read().decode('utf-8-sig'))))
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['评价备注'],"'=SUM(1,2)")
        self.assertEqual(rows[0]['原始回答'],e['answers'][0]['text'])
        self.assertEqual(rows[1]['评价人'],'')
        with request.urlopen(self.url+f'/api/experiments/{e["id"]}/export.json') as r:
            self.assertIn('.json',r.headers['Content-Disposition'])
            exported=json.load(r)
        self.assertEqual(exported['reviews'][0]['notes'],'=SUM(1,2)')
        self.assertEqual(exported['answers'],e['answers'])

if __name__ == '__main__':
    unittest.main()
