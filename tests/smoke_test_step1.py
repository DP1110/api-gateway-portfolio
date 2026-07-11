import urllib.request, json

def get(url):
    r = urllib.request.urlopen(url)
    return r.status, json.loads(r.read())

# 1. Gateway health
s, d = get('http://localhost:8000/gateway/health')
print('[1] Health check:', s, '->', d)

# 2. Proxy: GET /users (forwarded to users-service)
s, d = get('http://localhost:8000/users')
names = [u['name'] for u in d['users']]
print('[2] GET /users via proxy:', s, '-> names:', names, 'instance:', d['instance'])

# 3. Proxy: GET /users/1 (forwarded)
s, d = get('http://localhost:8000/users/1')
print('[3] GET /users/1 via proxy:', s, '->', d)

print()
print('Step 1 PASSED: reverse proxy is working correctly!')
