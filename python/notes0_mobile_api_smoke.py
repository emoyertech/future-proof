import uuid
from fastapi.testclient import TestClient
import notes0

client = TestClient(notes0.app)


def check(name, cond):
    if not cond:
        raise AssertionError(name)
    print(f"PASS: {name}")

u1 = f"api_user_{uuid.uuid4().hex[:6]}"
u2 = f"api_peer_{uuid.uuid4().hex[:6]}"
pw = "pass1234"

r = client.post('/api/auth/register', data={'username': u1, 'password': pw})
check('api register user1', r.status_code == 200 and 'token' in r.json())
t1 = r.json()['token']

r = client.post('/api/auth/register', data={'username': u2, 'password': pw})
check('api register user2', r.status_code == 200 and 'token' in r.json())
t2 = r.json()['token']

r = client.post('/api/auth/login', data={'username': u1, 'password': pw})
check('api login user1', r.status_code == 200 and 'token' in r.json())
t1 = r.json()['token']

hdr1 = {'Authorization': f'Bearer {t1}'}
hdr2 = {'Authorization': f'Bearer {t2}'}

r = client.get('/api/me', headers=hdr1)
check('api me works', r.status_code == 200 and r.json()['username'] == u1)

title = f"mobile_locked_{uuid.uuid4().hex[:6]}"
r = client.post('/api/notes', headers=hdr1, data={'title': title, 'content': 'hello mobile', 'lock_password': '1234'})
check('api create locked note', r.status_code == 200 and r.json()['locked'] is True)
filename = r.json()['filename']

r = client.get(f'/api/notes/{filename}', headers=hdr2)
check('api locked note blocks other user', r.status_code == 403)

r = client.get(f'/api/notes/{filename}', headers=hdr2, params={'note_password': '1234'})
check('api locked note unlock by password', r.status_code == 200 and r.json()['content'] == 'hello mobile')

msg_text = f"mobile_msg_{uuid.uuid4().hex[:6]}"
r = client.post('/api/messages', headers=hdr1, data={'recipient_username': u2, 'message_text': msg_text})
check('api send message', r.status_code == 200 and r.json()['text'] == msg_text)

r = client.get('/api/messages', headers=hdr2, params={'mark_read': 'false'})
check('api recipient sees unread_before_mark', r.status_code == 200 and r.json()['unread_before_mark'] >= 1)

r = client.get('/api/messages', headers=hdr2, params={'mark_read': 'true'})
check('api recipient marks read', r.status_code == 200)

r = client.get('/api/messages', headers=hdr1, params={'mark_read': 'false'})
sent = r.json()['sent']
check('api sender sees read receipt', any(m['text'] == msg_text and m['read'] for m in sent))

print('MOBILE_API_SMOKE_PASS')
