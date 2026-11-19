#!/usr/bin/env python3
"""Job watch: checks company job boards for Seattle-area engineering jobs and
internships and writes docs/jobs.json for the phone app (docs/index.html).
Runs on GitHub Actions twice a day (.github/workflows/watch.yml).

  python3 watch.py            check the boards and update the page
  python3 watch.py --dry-run  show what's new right now, save nothing
  python3 watch.py --reset    forget everything (next run starts a fresh list)

Companies and filters live in companies.json. History lives in data/seen.json.
"""
import json, os, re, subprocess, sys, time, urllib.parse
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, 'data')
STATE = os.path.join(STATE_DIR, 'seen.json')
DATA = os.path.join(HERE, 'docs', 'jobs.json')
TZ = ZoneInfo('America/Los_Angeles')  # GitHub runs in UTC; dates should be Seattle dates


def now():
    return datetime.now(TZ)
CFG = json.load(open(os.path.join(HERE, 'companies.json')))
UA = 'Mozilla/5.0 (Macintosh; job-watch personal script)'
F = {k: re.compile(v, re.I) for k, v in CFG['filters'].items()}
AMBIG = re.compile(r'\s*|\d+\s+locations|multiple( locations)?|various|united states|us|usa|remote', re.I)
SECTORS = [('energy', 'Energy'), ('aero', 'Aerospace and space'), ('other', 'Other engineering')]


# ---------- filters ----------
def title_ok(title):
    if F['exclude'].search(title):
        return False
    if F['engineer'].search(title):
        return True
    if F['intern'].search(title):  # "Mechanical Intern", or a bare "Internship 2027"
        rest = re.sub(r'summer|fall|winter|spring|internships?|interns?|co-?ops?|20\d\d|[-,()–]', '', title, flags=re.I)
        return bool(F['field'].search(title) or not rest.strip())
    return False


def local_ok(loc):
    """True if any listed site is in the Seattle area; None if the board doesn't say."""
    if AMBIG.fullmatch(loc.strip()):
        return None
    return any(F['local'].search(part) and not F['far'].search(part) for part in re.split(r';|\||/', loc))


# ---------- fetching ----------
def curl(url, data=None, timeout=40):
    cmd = ['curl', '-s', '-f', '-L', '-m', str(timeout), '-A', UA, '-H', 'Accept: application/json']
    if data is not None:
        cmd += ['-X', 'POST', '-H', 'Content-Type: application/json', '-d', json.dumps(data)]
    for attempt in range(3):  # boards like Microsoft's reset connections when busy
        r = subprocess.run(cmd + [url], capture_output=True, text=True)
        if r.returncode == 0:
            return json.loads(r.stdout)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f'fetch failed (curl exit {r.returncode})')


def greenhouse(c, cache):
    d = curl(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs")
    return [(str(j['id']), j['title'], (j.get('location') or {}).get('name', ''), j['absolute_url']) for j in d['jobs']]


def ashby(c, cache):
    d = curl(f"https://api.ashbyhq.com/posting-api/job-board/{c['slug']}")
    out = []
    for j in d['jobs']:
        locs = [j.get('location', '')] + [s.get('location', '') for s in (j.get('secondaryLocations') or [])]
        out.append((j['id'], j['title'], '; '.join(l for l in locs if l), j.get('jobUrl', '')))
    return out


def lever(c, cache):
    d = curl(f"https://api.lever.co/v0/postings/{c['slug']}?mode=json")
    return [(j['id'], j['text'], (j.get('categories') or {}).get('location', ''), j['hostedUrl']) for j in d]


def breezy(c, cache):
    d = curl(f"https://{c['slug']}.breezy.hr/json")
    return [(j['id'], j['name'], (j.get('location') or {}).get('name', ''), j['url']) for j in d]


def rippling(c, cache):
    d = curl(f"https://api.rippling.com/platform/api/ats/v1/board/{c['slug']}/jobs")
    return [(j['uuid'], j['name'], (j.get('workLocation') or {}).get('label', ''), j['url']) for j in d]


def ukg(c, cache):
    body = {"opportunitySearch": {"Top": 300, "Skip": 0, "QueryString": "", "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}],
            "Filters": [{"t": "TermsSearchFilterDto", "fieldName": n, "extra": None, "values": []} for n in (4, 5, 6)]},
            "matchCriteria": {"PreferredJobs": [], "Educations": [], "LicenseAndCertifications": [], "Skills": [], "hasNoLicenses": False, "SkippedSkills": []}}
    d = curl(c['board'] + '/JobBoardView/LoadSearchResults', data=body)
    out = []
    for o in d['opportunities']:
        addrs = [l.get('Address') or {} for l in o.get('Locations', [])]
        loc = '; '.join(f"{a.get('City') or ''}, {(a.get('State') or {}).get('Code') or ''}" for a in addrs)
        out.append((o['Id'], o['Title'], loc, c['board'] + '/OpportunityDetail?opportunityId=' + o['Id']))
    return out


def workday(c, cache):
    t, wd, site = c['tenant'], c['wd'], c['site']
    api = f"https://{t}.{wd}.myworkdayjobs.com/wday/cxs/{t}/{site}/jobs"
    seen = {}
    for term in c.get('search_terms', CFG['workday_terms']):
        off, total = 0, None
        while off < c.get('max_per_term', 600):
            d = curl(api, data={"limit": 20, "offset": off, "searchText": term, "appliedFacets": {}})
            posts = d.get('jobPostings', [])
            if total is None:  # Workday only reports the total on the first page
                total = d.get('total', 0)
            for j in posts:
                p = j.get('externalPath', '')
                seen[p] = [p, j.get('title', ''), j.get('locationsText', ''), f"https://{t}.{wd}.myworkdayjobs.com/{site}{p}"]
            off += 20
            if not posts or off >= total:
                break
            time.sleep(0.2)
    for v in seen.values():  # "3 Locations" -> look up the real sites (cached), only for titles that pass
        if title_ok(v[1]) and local_ok(v[2]) is None:
            key = f"{t}{v[0]}"
            if key not in cache:
                try:
                    info = curl(f"https://{t}.{wd}.myworkdayjobs.com/wday/cxs/{t}/{site}{v[0]}")['jobPostingInfo']
                    cache[key] = '; '.join([info.get('location', '')] + (info.get('additionalLocations') or []))
                except Exception:
                    continue
            v[2] = cache[key]
    return [tuple(v) for v in seen.values()]


def amazon(c, cache):
    seen = {}
    for term in CFG['amazon_terms']:
        off = 0
        while off < 300:
            q = urllib.parse.urlencode({'base_query': term, 'loc_query': 'Seattle, WA, United States', 'radius': '80km', 'result_limit': 100, 'offset': off, 'sort': 'recent'})
            d = curl('https://www.amazon.jobs/en/search.json?' + q)
            for j in d.get('jobs', []):
                seen[j['id_icims']] = (j['id_icims'], j['title'], j.get('normalized_location') or j.get('location', ''), 'https://www.amazon.jobs' + j['job_path'])
            off += 100
            if off >= d.get('hits', 0):
                break
    return list(seen.values())


def smartrecruiters(c, cache):
    out, off = [], 0
    while off < 1000:
        d = curl(f"https://api.smartrecruiters.com/v1/companies/{c['slug']}/postings?limit=100&offset={off}")
        for j in d.get('content', []):
            l = j.get('location') or {}
            loc = ', '.join(filter(None, [l.get('city'), l.get('region')]))
            out.append((j['id'], j['name'], loc, f"https://jobs.smartrecruiters.com/{c['slug']}/{j['id']}"))
        off += 100
        if off >= d.get('totalFound', 0):
            break
    return out


def workable(c, cache):
    d = curl(f"https://apply.workable.com/api/v1/widget/accounts/{c['slug']}")
    return [(j['shortcode'], j['title'], ', '.join(filter(None, [j.get('city'), j.get('state')])), j.get('url') or f"https://apply.workable.com/{c['slug']}/j/{j['shortcode']}/")
            for j in d.get('jobs', [])]


def eightfold(c, cache):
    seen = {}
    for q in c.get('queries', [c.get('query', 'engineer')]):
        start = 0
        while start < c.get('max', 600):
            loc = c.get('location', None if c.get('query') else 'Seattle')
            qs = urllib.parse.urlencode({'domain': c['domain'], 'query': q, 'start': start, **({'location': loc} if loc else {})})
            d = curl(f"https://{c['host']}/api/pcsx/search?{qs}")['data']
            pos = d.get('positions', [])
            for p in pos:
                loc = '; '.join((p.get('standardizedLocations') or []) + (p.get('locations') or []))
                seen[p['id']] = (str(p['id']), p['name'], loc, f"https://{c['host']}{p.get('positionUrl', '')}")
            start += len(pos) or 10
            if not pos or start >= d.get('count', 0):
                break
            time.sleep(1.0)  # these boards rate-limit
    return list(seen.values())


def oracle(c, cache):
    seen = {}
    for q in c.get('queries', [c.get('query', 'engineer')]):
        off = 0
        while off < 2000:
            u = (f"https://{c['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
                 f"&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber={c['site']},"
                 f"keyword={urllib.parse.quote(q)},limit=200,offset={off}")
            d = curl(u)['items'][0]
            reqs = d.get('requisitionList', [])
            for r in reqs:
                locs = [r.get('PrimaryLocation') or ''] + [x.get('Name', '') for x in r.get('secondaryLocations') or []]
                seen[r['Id']] = (r['Id'], r['Title'], '; '.join(filter(None, locs)),
                                 f"https://{c['host']}/hcmUI/CandidateExperience/en/sites/{c['site']}/job/{r['Id']}")
            off += 200
            if not reqs or off >= d.get('TotalJobsCount', 0):
                break
    return list(seen.values())


def jsonfeed(c, cache):  # Starcloud-style feed: {"jobs":[{reference,title,location,url}]}
    d = curl(c['url'])
    return [(j.get('reference') or j.get('id'), j['title'], '; '.join(j.get('locations') or [j.get('location', '')]),
             j.get('url') or j.get('applyUrl') or c.get('careers_url', '')) for j in d.get('jobs', [])]


def gem(c, cache):
    d = curl(f"https://api.gem.com/job_board/v0/{c['slug']}/job_posts/")
    return [(j['id'], j['title'], (j.get('location') or {}).get('name', ''), j['absolute_url']) for j in d]


def dejobs(c, cache):  # DirectEmployers feed: covers many defense, maritime and federal employers
    q = urllib.parse.urlencode({'location': 'WA', 'company': c['company'], 'num_items': 500})
    d = curl('https://dejobs.org/jobs/feed/json/?' + q)
    items = d if isinstance(d, list) else d.get('jobs') or d.get('items') or []
    return [(j.get('guid') or j.get('url'), j.get('title', ''), j.get('location', ''), j.get('url', '')) for j in items]


FETCH = dict(dejobs=dejobs, json=jsonfeed, gem=gem, smartrecruiters=smartrecruiters, workable=workable, eightfold=eightfold, oracle=oracle, greenhouse=greenhouse, ashby=ashby, lever=lever, breezy=breezy, rippling=rippling, ukg=ukg, workday=workday, amazon=amazon)


def sector_of(c, title):
    if c['ats'] == 'amazon':
        return 'aero' if re.search(r'\bleo\b|kuiper|prime air|satellite|avionics|space', title, re.I) else 'other'
    return c.get('sector', 'other')


def check(args):
    c, cache = args
    try:
        jobs = FETCH[c['ats']](c, cache)
        hits = [dict(id=f"{c['name']}|{i}", company=c['name'], sector=sector_of(c, t), title=' '.join(t.split()), location=l, url=u)
                for i, t, l, u in jobs if title_ok(t) and local_ok(l) is not False]
        return c['name'], len(jobs), hits, None
    except Exception as e:  # one broken board shouldn't stop the rest
        return c['name'], 0, [], str(e)


def log(msg):
    print(now().strftime('%Y-%m-%d %H:%M'), msg, flush=True)


# ---------- careers pages with no feed ----------
def page_text(url):
    r = subprocess.run(['curl', '-s', '-f', '-L', '-m', '40', '-A', 'Mozilla/5.0 (Macintosh)', url], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f'fetch failed (curl exit {r.returncode})')
    t = re.sub(r'(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>', ' ', r.stdout)
    t = re.sub(r'<[^>]+>', ' ', t)
    t = re.sub(r'&[a-z#0-9]+;', ' ', t)
    lines = [' '.join(x.split()) for x in re.split(r'\s{2,}', t)]
    return '\n'.join(x for x in lines if 2 < len(x) < 200)


def check_page(m):
    import hashlib
    try:
        text = page_text(m['url'])
        return m['name'], hashlib.sha1(text.encode()).hexdigest(), text, None
    except Exception as e:
        return m['name'], None, '', str(e)


# ---------- app data ----------
def is_intern(title):
    return bool(F['intern'].search(title))


def write_data(state, run_id, failed):
    """docs/jobs.json: everything the phone app shows."""
    # still open = seen this run, or its board couldn't be checked this run
    open_jobs = [j for j in state['seen'].values() if j.get('last_seen') == run_id or j['company'] in failed]
    jobs = [dict(company=j['company'], title=j['title'], url=j['url'], sector=j['sector'],
                 location=re.sub(r',?\s*(United States|USA)\b', '', j['location'] or ''),
                 intern=is_intern(j['title']), first_seen=j['first_seen'], id=j['id'])
            for j in open_jobs]
    jobs.sort(key=lambda j: (j['first_seen'], j['company'], j['title']), reverse=True)
    counts = {}
    for j in jobs:
        counts[j['company']] = counts.get(j['company'], 0) + 1
    cos = [dict(name=c['name'], sector=c.get('sector', 'aero' if c['ats'] == 'amazon' else 'other'), url=careers_url(c),
                open=counts.get(c['name'], 0), note=c.get('notes', '')) for c in CFG['companies']]
    pages = [dict(name=m['name'], url=m['url'], sector=m.get('sector', 'other'), note=m.get('why', ''),
                  changed=state['pages'].get(m['name'], {}).get('changed'), added=state['pages'].get(m['name'], {}).get('added', []))
             for m in CFG['manual']]
    out = dict(updated=now().isoformat(timespec='minutes'), baseline_date=state['baseline_date'],
               failed=failed, companies=cos, pages=pages, jobs=jobs)
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    tmp = DATA + '.tmp'
    json.dump(out, open(tmp, 'w'), separators=(',', ':'))
    os.replace(tmp, DATA)


def careers_url(c):
    a = c['ats']
    return dict(
        greenhouse=lambda: f"https://job-boards.greenhouse.io/{c['slug']}",
        ashby=lambda: f"https://jobs.ashbyhq.com/{c['slug']}",
        lever=lambda: f"https://jobs.lever.co/{c['slug']}",
        breezy=lambda: f"https://{c.get('slug')}.breezy.hr/",
        rippling=lambda: f"https://ats.rippling.com/{c.get('slug')}/jobs",
        ukg=lambda: c.get('board', ''),
        workday=lambda: f"https://{c.get('tenant')}.{c.get('wd')}.myworkdayjobs.com/{c.get('site')}",
        amazon=lambda: 'https://www.amazon.jobs/en/search?loc_query=Seattle%2C+WA',
        smartrecruiters=lambda: f"https://careers.smartrecruiters.com/{c.get('slug')}",
        workable=lambda: f"https://apply.workable.com/{c.get('slug')}/",
        dejobs=lambda: 'https://dejobs.org/jobs/?' + urllib.parse.urlencode({'location': 'WA', 'q': c.get('company', '')}),
        gem=lambda: f"https://jobs.gem.com/{c.get('slug')}",
        eightfold=lambda: f"https://{c.get('host')}/careers",
        oracle=lambda: f"https://{c.get('host')}/hcmUI/CandidateExperience/en/sites/{c.get('site')}/jobs",
    ).get(a, lambda: c.get('careers_url', ''))()


# ---------- main ----------
def main():
    dry = '--dry-run' in sys.argv
    os.makedirs(STATE_DIR, exist_ok=True)
    if '--reset' in sys.argv:
        if os.path.exists(STATE):
            os.remove(STATE)
        print('state cleared')
        return
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    state.setdefault('seen', {}); state.setdefault('fails', {}); state.setdefault('locs', {}); state.setdefault('pages', {})
    today = now().strftime('%Y-%m-%d')
    state.setdefault('baseline_date', today)
    run_id = now().replace(tzinfo=None).isoformat(timespec='seconds')

    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(check, [(c, state['locs']) for c in CFG['companies']]))
        page_results = list(ex.map(check_page, CFG['manual']))

    for name, digest, text, err in page_results:
        p = state['pages'].setdefault(name, {})
        if err or not digest:
            log(f'{name} (page): ERROR {err}')
            continue
        old = set((p.get('text') or '').splitlines())
        if p.get('hash') and p['hash'] != digest:
            added = [x for x in text.splitlines() if x not in old][:8]
            if added:  # ignore changes that only remove text
                p['changed'] = today
                p['added'] = added
                log(f'{name} (page): changed')
        p['hash'], p['text'] = digest, text

    new, failed = [], []
    for name, total, hits, err in results:
        if err:
            state['fails'][name] = state['fails'].get(name, 0) + 1
            failed.append(name)
            log(f'{name}: ERROR {err}')
            continue
        state['fails'][name] = 0
        log(f'{name}: {total} postings, {len(hits)} match')
        for j in hits:
            if j['id'] not in state['seen']:
                new.append(j)
                state['seen'][j['id']] = dict(j, first_seen=today)
            state['seen'][j['id']].update(last_seen=run_id, title=j['title'], location=j['location'], sector=j['sector'])

    if dry:
        for j in sorted(new, key=lambda j: ([s for s, _ in SECTORS].index(j['sector']), j['company'])):
            print(f"  NEW  [{j['sector']}] {j['company']}: {j['title']}  ({j['location']})")
        print(f'{len(new)} new (dry run: nothing saved)')
        return

    # forget postings that closed more than 60 days ago
    cutoff = (now().replace(tzinfo=None) - timedelta(days=60)).isoformat()
    state['seen'] = {k: v for k, v in state['seen'].items() if v.get('last_seen', '') >= cutoff}
    write_data(state, run_id, failed)
    json.dump(state, open(STATE, 'w'), indent=1)
    log(f'done: {len(new)} new, {len(failed)} errors')


if __name__ == '__main__':
    main()
