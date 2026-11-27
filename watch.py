#!/usr/bin/env python3
"""Job watch: checks company job boards for Seattle-area engineering jobs and
internships and writes docs/jobs.json for the phone app (docs/index.html).
Runs on GitHub Actions twice a day (.github/workflows/watch.yml).

  python3 watch.py            check the boards and update the page
  python3 watch.py --dry-run  show what's new right now, save nothing
  python3 watch.py --reset    forget everything (next run starts a fresh list)

Companies and filters live in companies.json. History lives in data/seen.json.
"""
import html, json, os, re, subprocess, sys, time, urllib.parse
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
    return any(F['local'].search(part) and not F['far'].search(part) and not OTHER_STATE.search(part)
               for part in re.split(r';|\||/', loc))


# A place name that also exists elsewhere (Washington, MO; Marysville, OH; Kent, OH; Auburn, AL) is only
# Seattle-area if no other state is named next to it.
_STATES = ('AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA '
           'RI SC SD TN TX UT VT VA WV WI WY').split()
_STATE_NAMES = ['Alabama', 'Alaska', 'Arizona', 'Arkansas', 'California', 'Colorado', 'Connecticut', 'Delaware', 'Florida', 'Georgia',
                'Hawaii', 'Idaho', 'Illinois', 'Indiana', 'Iowa', 'Kansas', 'Kentucky', 'Louisiana', 'Maine', 'Maryland', 'Massachusetts',
                'Michigan', 'Minnesota', 'Mississippi', 'Missouri', 'Montana', 'Nebraska', 'Nevada', 'New Hampshire', 'New Jersey',
                'New Mexico', 'New York', 'North Carolina', 'North Dakota', 'Ohio', 'Oklahoma', 'Oregon', 'Pennsylvania', 'Rhode Island',
                'South Carolina', 'South Dakota', 'Tennessee', 'Texas', 'Utah', 'Vermont', 'Virginia', 'West Virginia', 'Wisconsin',
                'Wyoming', 'District of Columbia', 'British Columbia', 'Ontario', 'England', 'Germany', 'India', 'Mexico', 'France']
OTHER_STATE = re.compile(r'(,|-|\s)\s*(' + '|'.join(_STATES) + r')\b(?!\w)|\b(' + '|'.join(_STATE_NAMES) + r')\b')


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
    d = curl(f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(c['slug'])}")
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
            time.sleep(c.get('delay', 1.0))  # these boards rate-limit
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


STAFFING = re.compile(r'actalent|system one|aerotek|insight global|eliassen|kforce|teksystems|randstad|robert half|jobot|apex systems|'
                      r'kelly services|adecco|manpower|collabera|cybercoders|modis|akkodis|yoh|beacon hill|motion recruitment|vaco|hays|'
                      r'experis|aston carter|talentbridge|staffing|recruit|pds tech|pds defense|butler america|volt|tata consultancy', re.I)
SWEEP_HW = re.compile(r'mechanical|manufactur|structur|civil|electrical|hardware|test|design|process|aerospace|propulsion|thermal|'
                      r'materials|controls|mechatronic|robotic|naval|marine|industrial|quality|reliability|tooling|production|'
                      r'project engineer|field engineer|facilities|HVAC|piping|plumbing|geotech|transportation|water|nuclear|'
                      r'power|grid|substation|battery|integration|validation|manufacturing|intern|co-op|fluid|composite|weld', re.I)
SWEEP_NOT = re.compile(r'services engineer|managed services|workspace|\bEUC\b|digital|consulting engineer|platform|site reliability|'
                       r'infrastructure engineer|administrator|cloud|devops|network|security|solutions|data center tech|'
                       r'technical account|customer|presales|pre-sales|support engineer|database|analytics|\bAI\b|\bML\b|'
                       r'machine learning|ux|user experience|product manager|program manager|test automation|qa engineer|'
                       r'quality assurance|sdet|front.?end|back.?end|full.?stack|mobile|ios|android|web', re.I)
AERO_WORDS = re.compile(r'aero|space|satellite|aircraft|aviation|avionic|rocket|propulsion|defense|naval|navy|fleet|missile|flight|boeing|airline', re.I)
ENERGY_WORDS = re.compile(r'energy|power|nuclear|fusion|solar|wind|battery|grid|utility|electric co|hydrogen|vernova|framatome|reactor|pnnl|national lab', re.I)


def dejobs_all(c, cache):
    """Every engineering job DirectEmployers has in Washington, from any company.
    Catches employers that aren't on our list or whose own site blocks us."""
    out, seen = [], set()
    for q in c.get('queries', ['engineer', 'engineering intern', 'engineering co-op']):
        off = 0
        while off < c.get('max', 8000):
            d = curl('https://dejobs.org/jobs/feed/json/?' + urllib.parse.urlencode({'q': q, 'location': 'WA', 'num_items': 500, 'offset': off}), timeout=90)
            fresh = [j for j in d if j.get('guid') not in seen]
            for j in fresh:
                seen.add(j.get('guid'))
                co = (j.get('company') or '').strip()
                if not co or STAFFING.search(co):
                    continue
                out.append((j.get('guid'), j.get('title', ''), j.get('location', ''), j.get('url', ''), co, (j.get('date_new') or '')[:10]))
            if len(d) < 500 or not fresh:
                break
            off += 500
    return out


def browser_json(url):
    """Fetch JSON through headless Chrome, which gets past Cloudflare-style bot checks. Mac only."""
    prof = os.path.expanduser('~/Library/Application Support/job-watch/chrome')
    os.makedirs(prof, exist_ok=True)
    chrome = next((p for p in ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', '/usr/bin/google-chrome'] if os.path.exists(p)), None)
    if not chrome:
        raise RuntimeError('no browser available')
    r = subprocess.run([chrome, '--headless=new', '--disable-gpu', f'--user-data-dir={prof}', '--virtual-time-budget=20000',
                        '--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36',
                        '--dump-dom', url], capture_output=True, text=True, timeout=120)
    m = re.search(r'(?s)<pre[^>]*>(.*)</pre>', r.stdout)
    if not m:
        raise RuntimeError('browser fetch returned no JSON')
    return json.loads(html.unescape(m.group(1)))


def talemetry(c, cache):
    """Talemetry career sites (Parker Hannifin): /jobs/search.json, behind a bot check, so read through Chrome."""
    out, page = [], 1
    while page <= 40:
        d = browser_json(f"https://{c['host']}/jobs/search.json?per_page=100&page={page}")
        for j in d.get('entries', []):
            l = j.get('location') or {}
            out.append((j['id'], j['title'], f"{(l.get('locality') or '').title()}, {l.get('region_abbr') or ''}",
                        f"https://{c['host']}/jobs/{j['id']}-{j.get('permalink', '')}"))
        if page * 100 >= d.get('total_entries', 0):
            break
        page += 1
    return out


FETCH = dict(talemetry=talemetry, dejobs_all=dejobs_all, dejobs=dejobs, json=jsonfeed, gem=gem, smartrecruiters=smartrecruiters, workable=workable, eightfold=eightfold, oracle=oracle, greenhouse=greenhouse, ashby=ashby, lever=lever, breezy=breezy, rippling=rippling, ukg=ukg, workday=workday, amazon=amazon)


def sector_of(c, title):
    if c['ats'] == 'amazon':
        return 'aero' if re.search(r'\bleo\b|kuiper|prime air|satellite|avionics|space', title, re.I) else 'other'
    return c.get('sector', 'other')


def _ckey(name):
    n = re.sub(r'\(.*?\)', '', name.lower())
    n = re.sub(r'\b(inc|llc|corp|corporation|company|co|ltd|the|usa|us|group|holdings|technologies|industries)\b', '', n)
    return re.sub(r'[^a-z0-9]', '', n)


_WATCH = None


def watched_by_board(company):
    """True if this company already has its own board on our list (so the sweep would only duplicate it)."""
    global _WATCH
    if _WATCH is None:
        _WATCH = {}
        for c in CFG['companies']:
            if c['ats'] in ('dejobs_all', 'adzuna'):
                continue
            for n in [c['name'], c.get('company', '')] + c.get('aliases', []):
                k = _ckey(n.split(' / ')[0])
                if len(k) >= 3:
                    _WATCH[k] = c.get('sector', 'other')
    k = _ckey(company)
    return len(k) >= 3 and any(k == w or (len(w) >= 4 and (k.startswith(w) or w.startswith(k))) for w in _WATCH)


def sector_for_company(company, title):
    k = _ckey(company)
    for c in CFG['companies'] + CFG['manual']:
        w = _ckey(c['name'])
        if len(w) >= 4 and (k.startswith(w) or w.startswith(k)):
            return c.get('sector', 'other')
    if ENERGY_WORDS.search(company):
        return 'energy'
    if AERO_WORDS.search(company + ' ' + title):
        return 'aero'
    return 'other'


def check(args):
    c, cache = args
    try:
        jobs = FETCH[c['ats']](c, cache)
        hits = []
        for row in jobs:
            i, t, l, u = row[:4]
            if not (title_ok(t) and local_ok(l) is not False):
                continue
            if len(row) > 4:  # catch-all feed: the row names its own company
                co = row[4]
                if not SWEEP_HW.search(t) or SWEEP_NOT.search(t):
                    continue  # big mixed feed: keep only clearly hardware-side titles
                if watched_by_board(co):
                    continue  # already covered by that company's own board
                hits.append(dict(id=f"{'AZ' if c['ats'] == 'adzuna' else 'ALL'}|{i}", company=co, sector=sector_for_company(co, t), title=' '.join(t.split()),
                                 location=l, url=u, source='sweep', posted=row[5] if len(row) > 5 else ''))
            else:
                hits.append(dict(id=f"{c['name']}|{i}", company=c['name'], sector=sector_of(c, t), title=' '.join(t.split()), location=l, url=u))
        return c['name'], len(jobs), hits, None
    except Exception as e:  # one broken board shouldn't stop the rest
        return c['name'], 0, [], str(e)


def log(msg):
    print(now().strftime('%Y-%m-%d %H:%M'), msg, flush=True)


# ---------- careers pages with no feed ----------
CHROME = next((p for p in ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', '/usr/bin/google-chrome',
                           '/usr/bin/google-chrome-stable', '/usr/bin/chromium-browser'] if os.path.exists(p)), None)
JOBLINK = re.compile(r'/(careers?|jobs?|positions?|openings?|opportunities|join-us|roles?)/[^/?#]*[a-z][^/?#]{3,}|'
                     r'greenhouse\.io/.+/jobs/\d|lever\.co/[^/]+/[0-9a-f-]{20,}|ashbyhq\.com/[^/]+/[0-9a-f-]{20,}|'
                     r'workable\.com/.+/j/|bamboohr\.com/careers/\d|paylocity\.com/.+/Details/|breezy\.hr/p/|'
                     r'rippling\.com/.+/jobs/|jobvite\.com/.+/job/|icims\.com/jobs/\d|recruitee\.com/o/|myworkdayjobs\.com/.+/job/|'
                     r'applytojob\.com/apply/[A-Za-z0-9]+/|paycomonline\.net/.+/jobs/\d|taleo\.net/.+(requisition\.jsp|jobdetail\.ftl|viewRequisition)|'
                     r'ttcportals\.com/jobs/\d|successfactors\.\w+/.+career_job_req_id|careerplug\.com/jobs/\d', re.I)


def get_html(url, render=False):
    if not render:
        r = subprocess.run(['curl', '-s', '-f', '-L', '-m', '40', '-A', 'Mozilla/5.0 (Macintosh)', url], capture_output=True, text=True)
        if r.returncode == 0 and len(r.stdout) > 500:
            return r.stdout
    if not CHROME:  # page needs a real browser (scripts or a picky server) and none is installed
        raise RuntimeError('fetch failed and no browser available')
    r = subprocess.run([CHROME, '--headless=new', '--disable-gpu', '--no-sandbox', '--virtual-time-budget=15000',
                        '--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36',
                        '--dump-dom', url], capture_output=True, text=True, timeout=90)
    if r.returncode != 0 or len(r.stdout) < 500:
        raise RuntimeError('browser fetch failed')
    return r.stdout


def page_text(html_src):
    t = re.sub(r'(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>', ' ', html_src)
    t = re.sub(r'<[^>]+>', ' ', t)
    t = re.sub(r'&[a-z#0-9]+;', ' ', t)
    lines = [' '.join(x.split()) for x in re.split(r'\s{2,}', t)]
    return '\n'.join(x for x in lines if 2 < len(x) < 200)


def ld_jobs(html_src, base):
    """JobPosting data that sites embed for Google's job search."""
    out = []
    for blob in re.findall(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html_src):
        try:
            d = json.loads(blob.strip())
        except Exception:
            continue
        stack = d if isinstance(d, list) else [d]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack += x
                continue
            if not isinstance(x, dict):
                continue
            stack += [v for v in x.values() if isinstance(v, (list, dict))]
            if x.get('@type') == 'JobPosting' and x.get('title'):
                loc = x.get('jobLocation') or {}
                loc = loc[0] if isinstance(loc, list) and loc else loc
                addr = (loc.get('address') if isinstance(loc, dict) else {}) or {}
                where = ', '.join(filter(None, [addr.get('addressLocality'), addr.get('addressRegion')])) if isinstance(addr, dict) else ''
                out.append((x.get('url') or base, html.unescape(x['title']), where))
    return out


def link_jobs(html_src, base):
    """Links that look like individual job postings, with their link text."""
    out = []
    for href, text in re.findall(r'(?is)<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', html_src):
        href = urllib.parse.urljoin(base, html.unescape(href))
        title = ' '.join(html.unescape(re.sub(r'<[^>]+>', ' ', text)).split())
        if len(title) > 120:  # a whole job card is inside the link (Paycom etc.): its first text is the title
            pieces = [' '.join(html.unescape(p).split()) for p in re.split(r'<[^>]+>', text)]
            title = next((p for p in pieces if len(p) >= 4), '')
        if not JOBLINK.search(href) or len(title) < 4 or len(title) > 120:
            continue
        if re.fullmatch(r'(?i)(apply|apply now|learn more|view|details|see (all|more|open).*|careers?|jobs?|open (roles|positions)|view (all )?(jobs|openings|positions))', title):
            slug = re.sub(r'[-_]+', ' ', href.rstrip('/').rsplit('/', 1)[-1])
            title = slug if re.search(r'[a-z]{3}', slug) and not slug.isdigit() and not re.search(r'[?=&]', slug) else title
        out.append((href, title, ''))
    return out


def sitemap_jobs(url):
    """New job pages often appear in a site's sitemap before anywhere else."""
    root = '{0.scheme}://{0.netloc}'.format(urllib.parse.urlparse(url))
    r = subprocess.run(['curl', '-s', '-f', '-L', '-m', '30', '-A', 'Mozilla/5.0 (Macintosh)', root + '/sitemap.xml'], capture_output=True, text=True)
    if r.returncode != 0:
        return []
    locs = re.findall(r'<loc>\s*([^<\s]+)\s*</loc>', r.stdout)
    if any(l.endswith('.xml') for l in locs) and len(locs) < 40:  # sitemap index: look one level down for job sitemaps
        for sub in [l for l in locs if re.search(r'job|career|position', l, re.I)][:3]:
            rr = subprocess.run(['curl', '-s', '-f', '-L', '-m', '30', sub], capture_output=True, text=True)
            locs += re.findall(r'<loc>\s*([^<\s]+)\s*</loc>', rr.stdout)
    out = []
    for l in locs:
        if JOBLINK.search(l) and not l.endswith('.xml'):
            slug = re.sub(r'[-_]+', ' ', l.rstrip('/').rsplit('/', 1)[-1])
            if re.search(r'[a-z]{4}', slug):
                out.append((l, slug, ''))
    return out[:300]


NONLOCAL = re.compile(r'\b(UK|United Kingdom|England|Kemble|London|Germany|France|India|Canada|Mexico|Japan|China|Remote)\b|, (TX|CA|CO|FL|AL|AZ|OH|MI|MO|TN|PA|NY|VA|NC|GA|IL|OR|UT|SC|IN|KY)\b')


def page_job_ok(title):
    t = title.lower()
    if NONLOCAL.search(title):  # careers pages often list other offices' jobs
        return False
    # titles scraped from careers pages: require an engineering word, since there's no job category to go on
    return not F['exclude'].search(title) and bool(F['engineer'].search(t) or (F['intern'].search(t) and F['field'].search(t))
                                                   or re.fullmatch(r'(?i)(summer |fall )?(20\d\d )?(engineering )?intern(ship)?s?( 20\d\d)?', title.strip()))


def check_page(m):
    import hashlib
    try:
        src = get_html(m['url'], m.get('render', False))
        found = ld_jobs(src, m['url']) + link_jobs(src, m['url'])
        if m.get('jobs') is False:  # page lists jobs from every office with no location: just watch it for changes
            found = []
        elif not found and not m.get('render') and CHROME:  # maybe the jobs are drawn by scripts
            try:
                src2 = get_html(m['url'], render=True)
                found = ld_jobs(src2, m['url']) + link_jobs(src2, m['url'])
                src = src2 if found else src
            except Exception:
                pass
        found += sitemap_jobs(m['url'])
        jobs, seen = [], set()
        for u, t, loc in found:
            key = u.split('?')[0].rstrip('/')
            if key in seen or not page_job_ok(t):
                continue
            seen.add(key)
            t = re.sub(r'\s*(Read more|Learn more|Apply( now)?)\s*$', '', t, flags=re.I)
            jobs.append(dict(id=f"{m['name']}|{key}", company=m['name'], sector=m.get('sector', 'other'),
                             title=t[:1].upper() + t[1:], location=loc or 'See posting', url=u, source='page'))
        text = page_text(src)
        return m['name'], hashlib.sha1(text.encode()).hexdigest(), text, None, jobs
    except Exception as e:
        return m['name'], None, '', str(e), []


# ---------- Hacker News "Who is hiring?" ----------
HW = re.compile(r'mechanical|hardware|aerospace|\bspace\b|satellite|rocket|propulsion|fusion|nuclear|fission|reactor|battery|batteries|'
                r'energy|robot|manufactur|drone|\buav\b|defense|electrical engineer|test engineer|climate|grid|solar|hydrogen|semiconductor', re.I)


def hn_jobs():
    d = curl('https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=10')
    story = next((h for h in d['hits'] if h['title'].lower().startswith('ask hn: who is hiring')), None)
    if not story:
        return []
    item = curl(f"https://hn.algolia.com/api/v1/items/{story['objectID']}", timeout=90)
    out = []
    for ch in item.get('children') or []:
        raw = ch.get('text') or ''
        txt = html.unescape(re.sub(r'<[^>]+>', ' ', raw.replace('<p>', '\n')))
        first = ' '.join(txt.strip().split('\n')[0].split())
        if not first or not F['local'].search(txt[:600]) or not HW.search(txt):
            continue
        if F['exclude'].search(first) and not HW.search(first):  # e.g. a software-only or senior-only post
            continue
        parts = [p.strip() for p in first.split('|')]
        company = parts[0][:60] or 'Startup'
        title = ' | '.join(parts[1:])[:140] or first[:140]
        sector = 'energy' if re.search(r'fusion|nuclear|fission|reactor|battery|energy|grid|solar|hydrogen|climate', txt, re.I) else \
                 'aero' if re.search(r'aerospace|\bspace\b|satellite|rocket|propulsion|drone|aircraft|aviation|defense', txt, re.I) else 'other'
        out.append(dict(id=f"HN|{ch['id']}", company=company, sector=sector, title=title, location='HN Who is hiring, ' + story['title'][24:].strip(' ()'),
                        url=f"https://news.ycombinator.com/item?id={ch['id']}", source='hn'))
    return out


# ---------- news: funding and new sites, a sign hiring is coming ----------
NEWS_QUERIES = [
    '(Seattle OR Bellevue OR Redmond OR Kirkland OR Bothell OR Everett OR Kent OR Tukwila OR Woodinville OR Tacoma) startup raises (space OR fusion OR nuclear OR battery OR aerospace OR drone OR robotics OR hardware OR energy OR defense)',
    'site:geekwire.com raises (space OR fusion OR nuclear OR battery OR aerospace OR drone OR robotics OR hardware OR climate OR energy)',
    'Washington state (new facility OR opens factory OR expands headquarters) (aerospace OR space OR energy OR manufacturing) Seattle',
]


def news():
    items = {}
    for q in NEWS_QUERIES:
        u = 'https://news.google.com/rss/search?' + urllib.parse.urlencode({'q': q + ' when:14d', 'hl': 'en-US', 'gl': 'US', 'ceid': 'US:en'})
        r = subprocess.run(['curl', '-s', '-f', '-L', '-m', '40', '-A', UA, u], capture_output=True, text=True)
        if r.returncode != 0:
            continue
        for it in re.findall(r'(?s)<item>(.*?)</item>', r.stdout):
            g = lambda tag: html.unescape((re.search(rf'(?s)<{tag}[^>]*>(.*?)</{tag}>', it) or [None, ''])[1]).strip()
            title, link, src, date = g('title'), g('link'), g('source'), g('pubDate')
            title = re.sub(r'\s+-\s+[^-]+$', '', title) if src and title.endswith(src) else title
            key = re.sub(r'[^a-z0-9]', '', title.lower())[:60]
            if key and key not in items:
                items[key] = dict(id='news|' + key, title=title, url=link, source=src, published=date)
    return list(items.values())


# ---------- app data ----------
def write_summary(jobs, pages, state, cos):
    """docs/summary.txt: a few plain lines the iPhone Shortcut reads out."""
    today = now().strftime('%Y-%m-%d')
    new = [j for j in jobs if j['first_seen'] == today and today != state['baseline_date']]
    order = {'energy': 0, 'aero': 1, 'other': 2}
    new.sort(key=lambda j: (order.get(j['sector'], 3), not j['intern'], j['company']))
    lines = [now().strftime('%A %b %-d')]
    if new:
        by = {k: sum(j['sector'] == k for j in new) for k in order}
        interns = sum(j['intern'] for j in new)
        lines.append(f"{len(new)} new role{'s' if len(new) > 1 else ''} today: {by['energy']} energy, {by['aero']} aero/space, {by['other']} other"
                     + (f", {interns} internship{'s' if interns > 1 else ''}" if interns else ''))
        lines += [f"- {j['company'].split(' (')[0]}: {j['title']}" for j in new[:8]]
        if len(new) > 8:
            lines.append(f"...and {len(new) - 8} more")
    else:
        lines.append(f"No new roles yet today. {len(jobs)} open across {len(cos)} companies.")
    changed = [p['name'].split(' (')[0] for p in pages if p.get('changed') == today]
    if changed:
        lines.append('Careers pages that changed: ' + ', '.join(changed))
    fresh = [n for n in state.get('news', {}).values() if n['first_seen'] == today and today != state['baseline_date']]
    if fresh:
        lines.append(f"{len(fresh)} news stor{'ies' if len(fresh) > 1 else 'y'} about Seattle hardware companies raising money or expanding (News tab)")
    lines.append('Tap OK to open the full list.')
    open(os.path.join(os.path.dirname(DATA), 'summary.txt'), 'w').write('\n'.join(lines) + '\n')



def is_intern(title):
    return bool(F['intern'].search(title))


def write_data(state, run_id, failed, soft=()):
    """docs/jobs.json: everything the phone app shows."""
    # still open = seen this run, or its board couldn't be checked this run
    open_jobs = [j for j in state['seen'].values() if j.get('last_seen') == run_id or j['company'] in failed or j['company'] in soft]
    jobs = [dict(company=j['company'], title=j['title'], url=j['url'], sector=j['sector'],
                 location=re.sub(r',?\s*(United States|USA)\b', '', j['location'] or ''),
                 intern=is_intern(j['title']), first_seen=j['first_seen'], id=j['id'], source=j.get('source', 'board'))
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
               failed=failed, companies=cos, pages=pages, jobs=jobs,
               news=sorted(state.get('news', {}).values(), key=lambda n: n['first_seen'], reverse=True)[:120])
    write_summary(jobs, pages, state, cos)
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
        talemetry=lambda: f"https://{c.get('host')}/jobs/search",
        dejobs=lambda: 'https://dejobs.org/jobs/?' + urllib.parse.urlencode({'location': 'WA', 'q': c.get('company', '')}),
        gem=lambda: f"https://jobs.gem.com/{c.get('slug')}",
        eightfold=lambda: f"https://{c.get('host')}/careers",
        oracle=lambda: f"https://{c.get('host')}/hcmUI/CandidateExperience/en/sites/{c.get('site')}/jobs",
    ).get(a, lambda: c.get('careers_url', ''))()


# ---------- boards that block cloud servers ----------
# Some boards (DirectEmployers, Microsoft) refuse GitHub's servers. The Mac checks
# just those (watch.py --mac, from a LaunchAgent) and pushes data/mac.json; the cloud
# run uses those results if they're fresh, otherwise keeps those roles as they were.
MAC = os.path.join(STATE_DIR, 'mac.json')


def run_mac():
    mac = [c for c in CFG['companies'] if c.get('via') == 'mac']
    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(check, [(c, {}) for c in mac]))
    for name, total, hits, err in res:
        log(f'{name}: ' + (f'ERROR {err}' if err else f'{total} postings, {len(hits)} match'))
    os.makedirs(STATE_DIR, exist_ok=True)
    json.dump(dict(fetched=now().isoformat(timespec='minutes'), results=res), open(MAC, 'w'), separators=(',', ':'))


def mac_results(mac):
    try:
        d = json.load(open(MAC))
        age = now() - datetime.fromisoformat(d['fetched'])
        fresh = age < timedelta(hours=30)
    except Exception:
        d, fresh = {'results': []}, False
    got = {r[0]: r for r in d['results']} if fresh else {}
    return [tuple(got[c['name']]) if c['name'] in got else (c['name'], 0, [], 'no fresh results from the Mac') for c in mac]


# ---------- main ----------
def main():
    dry = '--dry-run' in sys.argv
    if '--mac' in sys.argv:
        return run_mac()
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

    cloud = [c for c in CFG['companies'] if c.get('via') != 'mac']
    mac = [c for c in CFG['companies'] if c.get('via') == 'mac']
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(check, [(c, state['locs']) for c in cloud]))
        page_results = list(ex.map(check_page, CFG['manual']))
    results += mac_results(mac)
    try:
        hn = hn_jobs()
        results.append(('Hacker News', len(hn), hn, None))
    except Exception as e:
        log(f'Hacker News: ERROR {e}')
    try:
        for n in news():
            old_n = state.setdefault('news', {}).get(n['id'])
            state['news'][n['id']] = dict(n, first_seen=old_n['first_seen'] if old_n else today)
        ncut = (now() - timedelta(days=45)).strftime('%Y-%m-%d')
        state['news'] = {k: v for k, v in state['news'].items() if v['first_seen'] >= ncut}
    except Exception as e:
        log(f'news: ERROR {e}')

    soft = []  # sources that failed this run: keep their roles listed, but no warning in the app
    for name, digest, text, err, pjobs in page_results:
        p = state['pages'].setdefault(name, {})
        if err or not digest:
            log(f'{name} (page): ERROR {err}')
            soft.append(name)
            continue
        results.append((name, len(pjobs), pjobs, None))
        old = set((p.get('text') or '').splitlines())
        if p.get('hash') and p['hash'] != digest:
            # only flag new lines that look like a job title, so rotating banners and dates don't count
            added = [x for x in text.splitlines() if x not in old and len(x) < 100 and page_job_ok(x)
                     and not any(x.lower() in j['title'].lower() for j in pjobs)][:8]
            if added:
                p['changed'] = today
                p['added'] = added
                log(f'{name} (page): changed')
        p['hash'], p['text'] = digest, text

    # the two all-employer sweeps often list the same posting: keep the first copy
    def _dkey(j):
        return _ckey(j['company'])[:12] + '|' + re.sub(r'[^a-z0-9]', '', j['title'].lower())
    taken = set()
    for name, total, hits, err in results:
        if hits and hits[0].get('source') != 'sweep':
            taken |= {_dkey(j) for j in hits}
    for idx, (name, total, hits, err) in enumerate(results):
        if hits and hits[0].get('source') == 'sweep':
            keep = []
            for j in hits:
                k = _dkey(j)
                if k not in taken:
                    taken.add(k); keep.append(j)
            results[idx] = (name, total, keep, err)

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
                first = today
                if j.get('posted') and j['posted'] < today:
                    first = max(j['posted'], state['baseline_date'])  # posted before we saw it: file it under its posting day
                if first == today:
                    new.append(j)
                state['seen'][j['id']] = dict(j, first_seen=first)
            state['seen'][j['id']].update(last_seen=run_id, title=j['title'], location=j['location'], sector=j['sector'], source=j.get('source', 'board'))

    if dry:
        for j in sorted(new, key=lambda j: ([s for s, _ in SECTORS].index(j['sector']), j['company'])):
            print(f"  NEW  [{j['sector']}] {j['company']}: {j['title']}  ({j['location']})")
        print(f'{len(new)} new (dry run: nothing saved)')
        return

    # forget postings that closed more than 60 days ago
    cutoff = (now().replace(tzinfo=None) - timedelta(days=60)).isoformat()
    state['seen'] = {k: v for k, v in state['seen'].items() if v.get('last_seen', '') >= cutoff}
    write_data(state, run_id, failed, soft)
    json.dump(state, open(STATE, 'w'), indent=1)
    log(f'done: {len(new)} new, {len(failed)} errors')


if __name__ == '__main__':
    main()
