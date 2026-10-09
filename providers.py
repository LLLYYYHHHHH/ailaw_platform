"""Server-only configuration and streaming Chat Completions adapter."""
import json
import os
import socket
import time
from pathlib import Path
from urllib import request, error, parse

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / 'models.json'

class ProviderError(Exception):
    pass

def settings():
    # Local explicit configuration wins over inherited credentials (e.g. IDE).
    values = dict(os.environ)
    env_file = ROOT / '.env'
    if env_file.exists():
        for line in env_file.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip().strip('\"\'')
    try:
        configs = json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
        if not isinstance(configs, list) or len(configs) > 30:
            raise ValueError()
        models = {}
        for item in configs:
            if not isinstance(item, dict):
                raise ValueError()
            if item.get('enabled', True) is False:
                continue
            model_id = item['id']
            if not isinstance(model_id, str) or not model_id or model_id in models:
                raise ValueError()
            for field in ('name', 'model', 'base_url', 'api_key_env'):
                if not isinstance(item[field], str) or not item[field].strip():
                    raise ValueError()
            url = parse.urlsplit(item['base_url'])
            if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError()
            params = item.get('parameters', {})
            if item.get('api', 'chat_completions') not in ('chat_completions', 'responses'):
                raise ValueError()
            if not isinstance(params, dict) or any(k not in {'temperature', 'top_p', 'max_tokens', 'max_completion_tokens', 'max_output_tokens', 'seed', 'enable_thinking', 'reasoning_effort'} for k in params):
                raise ValueError()
            key = values.get(item['api_key_env'], '')
            models[model_id] = {**item, 'parameters': params, '_key': key, 'configured': bool(key and not key.startswith('YOUR_') and not item['model'].startswith('YOUR_'))}
        return models
    except (OSError, ValueError, KeyError, TypeError):
        raise ProviderError('模型配置文件格式错误，请检查 models.json') from None

def public_models(models):
    return [{k: m[k] for k in ('id', 'name', 'model', 'configured')} for m in models.values()]

class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def response_model(config, messages, answer):
    """Non-streaming Responses support, including Pro models."""
    params = dict(config['parameters'])
    budget = params.pop('max_completion_tokens', params.pop('max_tokens', None))
    if budget is not None:
        params.setdefault('max_output_tokens', budget)
    effort = params.pop('reasoning_effort', None)
    if effort:
        params['reasoning'] = {'effort': effort}
    body = {'model': config['model'], 'input': messages, 'stream': False, 'store': False, **params}
    answer['request_parameters'] = {k: v for k, v in body.items() if k != 'input'}
    answer['response_mode'] = 'non_streaming'
    req = request.Request(config['base_url'].rstrip('/') + '/responses', data=json.dumps(body, ensure_ascii=False).encode(), headers={'Authorization': 'Bearer ' + config['_key'], 'Content-Type': 'application/json', 'User-Agent': 'ModelEvalPlatform/0.3'}, method='POST')
    with request.build_opener(NoRedirect).open(req, timeout=300) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ProviderError('模型输出超过本次读取上限')
        data = json.loads(raw)
    if not isinstance(data, dict):
        raise ProviderError('模型返回的数据格式不正确')
    answer['returned_model'] = data.get('model')
    answer['response_id'] = data.get('id')
    answer['usage'] = data.get('usage')
    for item in data.get('output', []):
        if item.get('type') == 'message':
            for content in item.get('content', []):
                if content.get('type') == 'output_text':
                    answer['text'] += content.get('text', '')
                elif content.get('type') == 'refusal':
                    answer['text'] += content.get('refusal', '')
    if data.get('status') == 'incomplete':
        answer['finish_reason'] = (data.get('incomplete_details') or {}).get('reason', 'incomplete')
        raise ProviderError('模型未完成回答，可能达到输出预算；已保留部分回答')
    if data.get('status') != 'completed' or data.get('error'):
        raise ProviderError('服务商未完成本次响应')
    if not answer['text']:
        raise ProviderError('模型未返回可显示的回答，请检查输出预算')
    answer.update(status='completed', finish_reason='stop')

def stream_model(config, messages, answer, on_change):
    started = time.monotonic()
    answer['status'] = 'running'
    on_change()
    body = {'model': config['model'], 'messages': messages, **config['parameters'], 'stream': True}
    if config.get('include_usage', True):
        body['stream_options'] = {'include_usage': True}
    answer['request_parameters'] = {k: v for k, v in body.items() if k != 'messages'}
    answer['base_url'] = config['base_url']
    ended = False
    try:
        if config.get('api') == 'responses':
            response_model(config, messages, answer)
            return
        req = request.Request(config['base_url'].rstrip('/') + '/chat/completions', data=json.dumps(body, ensure_ascii=False).encode(), headers={'Authorization': 'Bearer ' + config['_key'], 'Content-Type': 'application/json', 'Accept': 'text/event-stream', 'User-Agent': 'ModelEvalPlatform/0.2'}, method='POST')
        opener = request.build_opener(NoRedirect)
        with opener.open(req, timeout=60) as response:
            if 'text/event-stream' not in response.headers.get('Content-Type', ''):
                raise ProviderError('接口没有返回流式数据，请检查兼容性和接口地址')
            data_lines = []
            size = 0
            while True:
                if time.monotonic() - started > 300:
                    raise ProviderError('生成超过 5 分钟，已停止读取；服务商可能仍产生费用')
                raw = response.readline(1048577)
                if len(raw) > 1048576:
                    raise ProviderError('模型返回的数据过大')
                size += len(raw)
                if size > 4 * 1024 * 1024:
                    raise ProviderError('模型输出超过本次读取上限')
                line = raw.decode('utf-8').rstrip('\r\n')
                if line.startswith('data:'):
                    data_lines.append(line[5:].lstrip())
                if (not line or not raw) and data_lines:
                    data = '\n'.join(data_lines)
                    data_lines = []
                    if data == '[DONE]':
                        ended = True
                        break
                    chunk = json.loads(data)
                    if not isinstance(chunk, dict) or chunk.get('error'):
                        raise ProviderError('服务商返回流式错误，请检查额度或模型配置')
                    if chunk.get('model'):
                        answer['returned_model'] = chunk['model']
                    if chunk.get('id'):
                        answer['response_id'] = chunk['id']
                    if chunk.get('system_fingerprint'):
                        answer['system_fingerprint'] = chunk['system_fingerprint']
                    if chunk.get('usage') is not None:
                        answer['usage'] = chunk['usage']
                    for choice in chunk.get('choices', []):
                        if choice.get('index', 0) != 0:
                            continue
                        text = (choice.get('delta') or {}).get('content') or ''
                        if not isinstance(text, str):
                            raise ProviderError('模型输出格式暂不支持')
                        if text:
                            if answer['first_token_ms'] is None:
                                answer['first_token_ms'] = round((time.monotonic() - started) * 1000)
                            answer['text'] += text
                        if choice.get('finish_reason'):
                            answer['finish_reason'] = choice['finish_reason']
                            ended = True
                    on_change()
                if not raw:
                    break
        if not ended:
            raise ProviderError('流式连接提前结束，已保留收到的部分回答')
        if not answer['text']:
            raise ProviderError('模型未返回可显示的回答，请检查输出预算或模型参数')
        answer['status'] = 'completed'
    except error.HTTPError as exc:
        reasons = {400: '请求参数不支持，请检查模型参数', 401: 'API 密钥无效或地域不匹配', 403: '无权限访问该模型', 404: '模型名称或接口地址不存在', 429: '调用限流或额度不足'}
        answer.update(status='failed', error=f'HTTP {exc.code}：' + reasons.get(exc.code, '服务商请求失败，请稍后重试'))
        answer['http_status'] = exc.code
        # Only retain known error codes; provider messages can contain secrets.
        try:
            details = json.loads(exc.read(65536))
            item = details.get('error') or {}
            known = {'insufficient_quota':'API 额度不足，请检查服务商账户余额和额度设置', 'rate_limit_exceeded':'调用限流，请稍后再试或检查速率限制', 'invalid_api_key':'API 密钥无效', 'model_not_found':'模型不存在或账号无访问权限', 'unsupported_country_region_territory':'当前访问地区不受服务商支持'}
            code = item.get('code')
            if not isinstance(code, str) or code not in known:
                code = item.get('type')
            if isinstance(code, str) and code in known:
                answer['error_code'] = code
                answer['error'] = f'HTTP {exc.code}：' + known[code]
        except (ValueError, TypeError, AttributeError, OSError):
            pass
        exc.close()
    except (TimeoutError, socket.timeout):
        answer.update(status='failed', error='连接或读取超时，已保留部分回答')
    except error.URLError as exc:
        if isinstance(exc.reason, PermissionError):
            answer.update(status='failed', error='当前运行进程无网络访问权限，请在允许联网的终端启动平台')
        else:
            answer.update(status='failed', error='无法连接模型服务，请检查网络和接口地址')
    except ProviderError as exc:
        answer.update(status='failed', error=str(exc))
    except (ValueError, UnicodeError, TypeError, AttributeError):
        answer.update(status='failed', error='模型返回的数据格式不正确')
    except Exception:
        answer.update(status='failed', error='调用异常，已保留收到的回答')
    finally:
        answer['elapsed_ms'] = round((time.monotonic() - started) * 1000)
        on_change()
