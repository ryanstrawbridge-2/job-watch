# Job Watch

A phone app listing every new engineering job and internship at ~180 Seattle-area
companies, energy and aerospace first. GitHub Actions checks the boards twice a day
(`.github/workflows/watch.yml`), commits `docs/jobs.json`, and GitHub Pages serves the
app from `docs/`. Open the Pages link in Safari on iPhone, then Share > Add to Home Screen.

- `companies.json`: companies with readable job boards (`companies`), careers pages
  watched for any change (`manual`), and the title/location filters.
- `watch.py`: fetchers for Greenhouse, Ashby, Lever, Workday, SmartRecruiters, Workable,
  Eightfold, Oracle, UKG, Breezy, Rippling, Gem, DirectEmployers, Amazon and a JSON feed.
- `data/seen.json`: history (first-seen dates, page snapshots).

Run locally: `python3 watch.py --dry-run`. Run in the cloud now: Actions tab > Check job
boards > Run workflow.

What counts: any title with "engineer"/"engineering", plus engineering internships and
co-ops; not senior/staff/principal/lead/manager, software, IT, data, technician or sales.
At least one location must be in the Seattle area.
