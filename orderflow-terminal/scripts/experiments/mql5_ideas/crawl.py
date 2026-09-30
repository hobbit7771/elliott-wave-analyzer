import re, json, time, subprocess, html, sys
def get(u):
    for i in range(3):
        r = subprocess.run(['curl','-sL','-m','30','-A','Mozilla/5.0',u], capture_output=True)
        if r.returncode == 0 and len(r.stdout) > 5000: return r.stdout.decode('utf-8','ignore')
        time.sleep(3)
    return ''
tile = re.compile(r'<div class="code-tile">(.*?)</div>\s*</div>|<div class="code-tile">(.*?)(?=<div class="code-tile">|<div class="paginator)', re.S)
out = {}
for plat in ('mt5','mt4'):
    for cat in ('experts','indicators','libraries','scripts'):
        base = f'https://www.mql5.com/ru/code/{plat}/{cat}'
        s = get(base); pages = max([1]+[int(x) for x in re.findall(rf'/ru/code/{plat}/{cat}/page(\d+)', s)])
        print(plat, cat, pages, flush=True)
        for p in range(1, pages+1):
            if p > 1: s = get(f'{base}/page{p}'); time.sleep(0.5)
            for m in re.finditer(r'class="g-rating g-rating_sm g-rating_v(\d+)".*?<a href="/ru/code/(\d+)" title="([^"]*)">.*?</div>\s*<p>(.*?)</p>', s, re.S):
                out[m.group(2)] = {'plat': plat, 'cat': cat, 'rating': int(m.group(1))/10, 'title': html.unescape(m.group(3)), 'desc': html.unescape(re.sub('<[^>]+>','',m.group(4))).strip()}
        json.dump(out, open('catalog.json','w'), ensure_ascii=False)
        print(' total', len(out), flush=True)
