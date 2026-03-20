import uuid
from fastapi.testclient import TestClient
import notes0

client = TestClient(notes0.app)


def check(name, cond):
    if not cond:
        raise AssertionError(name)
    print(f"PASS: {name}")

# create sender account
r = client.get('/')
csrf = r.cookies.get('csrf_token')
sender = f"msg_sender_{uuid.uuid4().hex[:6]}"
r = client.post('/auth/register', data={'username': sender, 'password': 'pass1234', 'csrf_token': csrf}, follow_redirects=False)
check('sender registered', r.status_code == 303)

# logout and create recipient account
client.get('/auth/logout')
r = client.get('/')
csrf = r.cookies.get('csrf_token')
recipient = f"msg_rec_{uuid.uuid4().hex[:6]}"
r = client.post('/auth/register', data={'username': recipient, 'password': 'pass1234', 'csrf_token': csrf}, follow_redirects=False)
check('recipient registered', r.status_code == 303)

# login sender and send message
client.get('/auth/logout')
r = client.get('/')
csrf = r.cookies.get('csrf_token')
r = client.post('/auth/login', data={'username': sender, 'password': 'pass1234', 'csrf_token': csrf}, follow_redirects=False)
check('sender login', r.status_code == 303)

r = client.get('/messages')
csrf = r.cookies.get('csrf_token')
check('messages page loads for sender', r.status_code == 200 and 'Send Message' in r.text)
text = f"hello_{uuid.uuid4().hex[:6]}"
r = client.post('/messages/send', data={'recipient_username': recipient, 'message_text': text, 'csrf_token': csrf}, follow_redirects=False)
check('message send redirect', r.status_code == 303)

r = client.get('/messages')
check('sender sees unread receipt first', r.status_code == 200 and text in r.text and 'Unread' in r.text)

# login recipient and verify inbox
client.get('/auth/logout')
r = client.get('/')
csrf = r.cookies.get('csrf_token')
r = client.post('/auth/login', data={'username': recipient, 'password': 'pass1234', 'csrf_token': csrf}, follow_redirects=False)
check('recipient login', r.status_code == 303)

r = client.get('/messages')
check('recipient inbox contains message', r.status_code == 200 and text in r.text and sender in r.text)

# sender sees read receipt after recipient opens messages
client.get('/auth/logout')
r = client.get('/')
csrf = r.cookies.get('csrf_token')
r = client.post('/auth/login', data={'username': sender, 'password': 'pass1234', 'csrf_token': csrf}, follow_redirects=False)
check('sender re-login', r.status_code == 303)

r = client.get('/messages')
check('sender sees read receipt', r.status_code == 200 and text in r.text and 'Read' in r.text)
print('MESSAGES_SMOKE_PASS')
