"""Exercise a running API without paid model services. All evidence is synthetic."""
import json
import os
from urllib.request import Request, urlopen

base = 'http://127.0.0.1:' + os.getenv('GEO_PORT', '8000')
token = os.environ['GEO_API_TOKEN']


def call(path, body=None):
    request = Request(base + '/api/v1/' + path,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urlopen(request, timeout=10) as response:
        return json.load(response)


sites = call('sites')
site = next((s for s in sites if s['origin'] == 'https://demo.example'), None)
if not site:
    site = call('sites', dict(name='星舟（合成演示）', url='https://demo.example', brands=['星舟', '竞品甲']))
prefix = 'sites/' + site['id']
if call(prefix + '/inventory') is None:
    call(prefix + '/inventory', dict(source='synthetic-demo', urls=[
        'https://demo.example/product', 'https://demo.example/missing', 'https://demo.example/unseen']))
call(prefix + '/logs', dict(adapter='json-v1', source='synthetic-demo', records=[
    dict(event_id='demo-log-1', observed_at='2026-10-29T09:00:00+08:00',
         url='https://demo.example/product', status=200, user_agent='GPTBot'),
    dict(event_id='demo-log-2', observed_at='2026-10-29T09:05:00+08:00',
         url='https://demo.example/missing', status=404, user_agent='GPTBot')]))
call(prefix + '/answers', dict(adapter='manual-v1', source='synthetic-demo', records=[
    dict(event_id='demo-answer-1', observed_at='2026-10-29T09:10:00+08:00', platform='synthetic',
         prompt='演示：有哪些产品？', answer='合成样例：星舟与竞品甲。', citations=['https://demo.example/product'])]))
print(json.dumps(call(prefix + '/report'), ensure_ascii=False, indent=2))
