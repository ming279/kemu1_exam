import requests
s = requests.Session()
s.post('http://127.0.0.1:5000/login', data={'username':'123456','password':'123456'})
r = s.get('http://127.0.0.1:5000/my_papers')
print('list:', r.status_code)
import re
ids = re.findall(r'data-pid="(\d+)"', r.text)
print('found paper ids:', ids[:3])
if ids:
    pid = ids[0]
    r2 = s.post(f'http://127.0.0.1:5000/exam_paper/{pid}/delete')
    print('delete pid', pid, ':', r2.status_code, r2.text[:100])
r3 = s.post('http://127.0.0.1:5000/exam_paper/99999/delete')
print('delete 99999:', r3.status_code, r3.text[:80])
