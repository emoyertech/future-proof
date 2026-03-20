import uuid
from fastapi.testclient import TestClient
import notes0

client = TestClient(notes0.app)


def check(name, cond):
    if not cond:
        raise AssertionError(name)
    print(f"PASS: {name}")

r = client.get('/')
csrf = r.cookies.get('csrf_token')
check('csrf exists', bool(csrf))

username = f"user_{uuid.uuid4().hex[:8]}"
password = "pass1234"
r = client.post('/auth/register', data={'username': username, 'password': password, 'csrf_token': csrf}, follow_redirects=False)
check('register redirect', r.status_code == 303)

r = client.get('/')
csrf = r.cookies.get('csrf_token')

note_title = f"locked_{uuid.uuid4().hex[:8]}"
r = client.post('/notes/create', data={'filename': note_title, 'lock_note': '1', 'lock_password': 'lockpw', 'csrf_token': csrf}, follow_redirects=False)
check('create locked redirect', r.status_code == 303)

note_name = note_title + '.md'
r = client.get(f'/notes/{note_name}')
check('owner auto access locked note', 'Locked note' not in r.text and r.status_code == 200)

r = client.post(f'/notes/{note_name}/unlock', data={'note_password': 'lockpw', 'csrf_token': csrf}, follow_redirects=False)
check('unlock still works', r.status_code == 303)

r = client.get(f'/notes/{note_name}')
check('locked note opens after unlock', r.status_code == 200 and 'Locked note' not in r.text)

r = client.get('/account')
check('account page loads', r.status_code == 200 and 'Change Password' in r.text)

csrf = r.cookies.get('csrf_token') or client.get('/').cookies.get('csrf_token')
r = client.post('/account/password', data={'current_password': password, 'new_password': 'pass9999', 'csrf_token': csrf}, follow_redirects=False)
check('password change redirect', r.status_code == 303)

# logout/login with new password
client.get('/auth/logout')
r = client.get('/')
csrf = r.cookies.get('csrf_token')
r = client.post('/auth/login', data={'username': username, 'password': 'pass9999', 'csrf_token': csrf}, follow_redirects=False)
check('login with new password', r.status_code == 303)

# admin page should not be accessible to non-admin user
r = client.get('/admin/users')
check('admin access blocked for user', r.status_code == 403)

print('AUTH_LOCK_SMOKE_PASS')
