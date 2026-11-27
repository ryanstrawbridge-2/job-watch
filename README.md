# Job Watch

A phone app listing every new engineering job and internship at ~180 Seattle-area
companies, energy and aerospace first. GitHub Actions checks the boards twice a day
(`.github/workflows/watch.yml`), commits `docs/jobs.json`, and GitHub Pages serves the
app from `docs/`. Open the Pages link in Safari on iPhone, then Share > Add to Home Screen.

- `companies.json`: companies with readable job boards (`companies`), careers pages
  watched for any change (`manual`), and the title/location filters.
- `watch.py`: fetchers for Greenhouse, Ashby, Lever, Workday, SmartRecruiters, Workable,
  Eightfold, Oracle, UKG, Breezy, Rippling, Gem, ADP, Talemetry, DirectEmployers, Adzuna,
  Amazon and a JSON feed. Workday boards are searched with their own Seattle-area location filter.
- `geo.py`: miles from downtown Seattle for Washington place names.
- `data/seen.json`: history (first-seen dates, page snapshots).

Run locally: `python3 watch.py --dry-run`. Run in the cloud now: Actions tab > Check job
boards > Run workflow.

What counts: any title with "engineer"/"engineering", plus engineering internships and
co-ops; not senior/staff/principal/lead/manager, III+ or 3+ years, software, IT, data,
technician or sales. At least one site must be within 50 miles of Seattle (straight line).
Each role gets a level (internship, entry, mid, not stated) and a distance for the app's filters.

The app (`docs/index.html`) has LinkedIn-style filters: date found, experience level,
engineering type, industry, distance and company, plus sort, saved/hidden roles, a "new since
your last visit" count and grouping of the same role posted in several cities.
