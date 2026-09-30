# MQL5 articles catalog (ru): id, title, description, category (from the category listings).
import re, json, time, subprocess, html
def get(u):
    for _ in range(3):
        r = subprocess.run(['curl','-sL','-m','30','-A','Mozilla/5.0',u], capture_output=True)
        if r.returncode == 0 and len(r.stdout) > 5000: return r.stdout.decode('utf-8','ignore')
        time.sleep(3)
    return ''
rx = re.compile(r'<a href="/ru/articles/(\d+)">([^<]+)</a>\s*</h3>\s*</header>\s*<div class="articles-item__description">(.*?)</div>', re.S)
A = {}
def crawl(base, cat):
    s = get(base); pages = max([1] + [int(x) for x in re.findall(re.escape(base.split('mql5.com')[1]) + r'/page(\d+)', s)])
    for p in range(1, pages + 1):
        if p > 1: s = get(f'{base}/page{p}'); time.sleep(0.4)
        for i, t, d in rx.findall(s):
            a = A.setdefault(i, {'title': html.unescape(t).strip(), 'desc': html.unescape(re.sub('<[^>]+>', '', d)).strip(), 'cats': []})
            if cat and cat not in a['cats']: a['cats'].append(cat)
    print(cat or 'all', pages, len(A), flush=True)
crawl('https://www.mql5.com/ru/articles', None)
for c in ('trading_systems','machine_learning','statistics','trading','strategy_tester','indicators','expert_advisors','integration','examples'):
    crawl(f'https://www.mql5.com/ru/articles/{c}', c)
json.dump(A, open('articles.json','w'), ensure_ascii=False)
