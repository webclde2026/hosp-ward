# Ward Rate Compass — Singapore Hospital Room & Board Rate Tracker

A single static HTML page comparing daily room & board rates across Singapore's
public/restructured and private hospitals, sourced directly from each
hospital's own website. Refreshes automatically once a month via GitHub
Actions — free, no API keys, no paid services.

## What's in here

```
data/hospitals.json        Source of truth: one entry per hospital, with its
                            ward rates, source URL, and retrieval date.
data/scrape_log.json       Written by each scraper run (git-ignored content,
                            tracked file) — what changed, what needs review.
scraper/scrape.py          Re-checks every hospital's source page monthly.
site/template.html         The page itself (HTML/CSS/JS, data embedded at build time).
site/build.py              Embeds data/hospitals.json into template.html ->
                            writes site/index.html AND docs/index.html.
docs/index.html             What GitHub Pages actually serves (see setup below).
.github/workflows/monthly-refresh.yml   The scheduled job.
```

## One-time setup (5 minutes)

1. **Create the repo.** Push this whole folder to a new GitHub repository
   (public repos get GitHub Pages + Actions for free; private repos need a
   paid plan for Pages, but Actions is still free either way).

2. **Turn on GitHub Pages.**
   Repo → **Settings** → **Pages** → under "Build and deployment", set
   **Source: Deploy from a branch**, **Branch: main**, **Folder: /docs**,
   then **Save**. Your site will be live at
   `https://<your-username>.github.io/<repo-name>/` within a minute or two.

3. **That's it.** No secrets, no API keys, nothing else to configure. The
   workflow already has the permissions it needs (`contents: write`,
   `issues: write`) declared in the workflow file itself.

## How the monthly refresh works

On the 1st of every month (and any time you trigger it manually from the
**Actions** tab → "Monthly rate refresh" → **Run workflow**):

1. `scraper/scrape.py` re-fetches every hospital's rate page.
   - For hospitals that publish rates as plain HTML text (most of them), it
     re-parses the table and updates the figures directly.
   - For the 3 hospitals whose rates are published as an **image**
     (National University Hospital, Alexandra Hospital, Ng Teng Fong
     General Hospital) — a real table image can't be safely auto-read
     without either a paid vision API or unreliable OCR, so this script
     doesn't try to read numbers off the image at all. Instead it just
     hashes the source page and compares it to last month's hash:
     - **Unchanged page** → nothing to do, last confirmed figures still stand.
     - **Changed page** → the hospital is flagged `needs_review` on the
       site (shown with the same amber "needs review" status as any other
       caveated entry) and the **last known figures are kept, never
       blanked or guessed**. A GitHub Issue is opened (or updated, if one's
       already open) listing exactly which hospitals changed and their
       source URLs.
   - If a fetch fails outright (site down, etc.) that hospital is marked
     `stale` and keeps its last known data — a bad network day never wipes
     out good data.
2. `site/build.py` rebuilds `docs/index.html` from the refreshed data.
3. If anything actually changed, the workflow commits and pushes it —
   GitHub Pages then republishes automatically.

## When you get a "needs review" issue

Open the source URL(s) listed in the issue, read the current rate table
(screenshot it if it's an image), and update that hospital's `wards` array
in `data/hospitals.json` by hand — same as how NUH/Alexandra/NTFGH's current
figures were originally captured. Set `data_status` back to `"ok"`, update
`retrieved_date`, run `python3 site/build.py`, and commit. The issue closes
itself automatically the next time the monthly job runs and finds nothing
outstanding.

## Running it locally / manually, any time

```bash
pip install requests beautifulsoup4
python3 scraper/scrape.py       # re-check every hospital now
python3 site/build.py           # rebuild the page from data/hospitals.json
open site/index.html            # preview locally
```

Use `python3 scraper/scrape.py --dry-run` to see what would change without
writing anything.

## Data integrity principles (kept from the original brief)

- Only publicly published rates from each hospital's own official page —
  never estimated, inferred, or fabricated.
- `robots.txt` is checked before every fetch.
- A failed or ambiguous scrape always **keeps the last known-good figures**
  and flags the row (`stale` / `needs_review`) rather than guessing or
  blanking it out.
- Every row shows its source URL and retrieval date so anyone can verify a
  figure independently.
