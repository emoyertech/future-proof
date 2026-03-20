import uuid
from fastapi.testclient import TestClient
import notes0

client = TestClient(notes0.app)


def check(name, condition, detail=''):
    if not condition:
        raise AssertionError(f"{name} failed: {detail}")
    print(f"PASS: {name}")


r = client.get('/')
check('GET / status', r.status_code == 200, r.status_code)
csrf = r.cookies.get('csrf_token')
check('csrf cookie exists', bool(csrf), csrf)

note_title = f"smoke_{uuid.uuid4().hex[:8]}"
r = client.post('/notes/create', data={'filename': note_title, 'csrf_token': csrf}, follow_redirects=False)
check('POST /notes/create redirect', r.status_code == 303, r.status_code)
note_name = note_title + '.md'

r = client.get(f'/notes/{note_name}')
check('GET note page', r.status_code == 200, r.status_code)

r = client.post(f'/notes/{note_name}/save', data={'content': '# updated body', 'csrf_token': csrf}, follow_redirects=False)
check('POST note save redirect', r.status_code == 303, r.status_code)

r = client.post(f'/notes/{note_name}/delete', data={'csrf_token': csrf}, follow_redirects=False)
check('POST note delete redirect', r.status_code == 303, r.status_code)

csv_name = f"smoke_{uuid.uuid4().hex[:8]}.csv"
csv_bytes = b"a,b\n1,2\n"
r = client.post('/datasets/import', data={'csrf_token': csrf}, files={'file': (csv_name, csv_bytes, 'text/csv')}, follow_redirects=False)
check('POST dataset import redirect', r.status_code == 303, r.status_code)

r = client.get(f'/datasets/{csv_name}/full')
check('GET dataset full view', r.status_code == 200, r.status_code)

video_name = f"smoke_{uuid.uuid4().hex[:8]}.mp4"
video_bytes = b'not-a-real-video-but-stored'
r = client.post('/videos/import', data={'csrf_token': csrf}, files={'file': (video_name, video_bytes, 'video/mp4')}, follow_redirects=False)
check('POST video import redirect', r.status_code == 303, r.status_code)

r = client.get(f'/videos/{video_name}')
check('GET video page', r.status_code == 200, r.status_code)

r = client.get(f'/videos/{video_name}/stream')
check('GET video stream', r.status_code == 200, r.status_code)
check('video stream bytes match', r.content == video_bytes, len(r.content))

r = client.post(f'/videos/{video_name}/delete', data={'csrf_token': csrf}, follow_redirects=False)
check('POST video delete redirect', r.status_code == 303, r.status_code)

r = client.get('/notes/../secrets.txt')
check('path traversal blocked', r.status_code in (400, 404), r.status_code)

r = client.post('/notes/create', data={'filename': 'x', 'csrf_token': 'wrong'}, follow_redirects=False)
check('csrf blocked', r.status_code == 403, r.status_code)

print('SMOKE_TEST_PASS')
