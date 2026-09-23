# Venue Calendar

Venue Calendar is a focused tracker for research conference deadlines and selected journals.

## Scope

- Conferences: CVPR, ICCV, ECCV, BMVC, WACV, ACM MM, ICASSP, ICIP, ICME, AAAI, IJCAI, ECAI, NeurIPS, ICML, and ICLR.
- Journals: TPAMI, IJCV, Artificial Intelligence, and Pattern Recognition.
- Deadlines are converted to the browser's local time zone.
- Search, ranking filters, and countdowns run entirely in the browser.

## Run locally

Open `index.html` directly, or start a local static server:

```bash
python3 -m http.server 8080
```

Then open <http://localhost:8080>.

## Data refresh

`scripts/refresh_official.py` starts from the stable official series hubs in
`data/official_sources.yml`. It then:

- probes known URL patterns without assuming that a future page already exists;
- discovers target-year links from official hubs, announcement pages, sitemaps,
  and previously verified editions, including links to new domains;
- remembers a bounded history of verified edition URLs across year changes;
- searches for the conference name and year through public DuckDuckGo Lite and
  Brave results on every daily check, even while the cached homepage still works,
  so newly corroborated replacement sites can take precedence;
- treats search hits as candidates only: new domains need a link from an official
  organizer or previously verified edition, plus a venue + year identity check;
- follows a bounded set of trusted, target-edition Dates and Call for Papers pages;
- reads official UTC countdowns and labelled submission dates, preserving their
  time zones and source evidence;
- lets newly verified official dates replace older dates, using reviewed registry
  values only as fallbacks;
- extracts high-confidence locations from Schema.org data, labelled official text,
  event banners, or explicit official future-meetings pages, excluding placeholders;
- keeps the last verified value when a site is temporarily unavailable; and
- requires two consecutive observations before replacing an existing location.

GitHub Actions runs this refresh once every 24 hours, scheduled for 21:17 UTC
(05:17 the following day in Beijing; GitHub may delay scheduled runs). Pushes to
`main` and manual workflow runs also refresh and deploy the site. The site remains
a static snapshot, checks for a new snapshot when opened or revisited, and reloads
it every 15 minutes while left open. These browser checks do not scrape conference
websites. An edition without a verified homepage links to its official series hub.
The page distinguishes an unfound homepage, an unverified candidate, and a
temporarily unavailable search. A failed lookup does not establish that a
conference has not announced its details.

Accepted deadlines and locations store their official source URLs and extraction
evidence in `data/refresh_state.json`. The workflow summary lists the results for
each conference so a successful deployment can be distinguished from missing data.
Each successful check saves its snapshot and check time to the repository, including
days without changed deadlines, keeping the scheduled repository active.
The generated `data/conferences.js` contains only the display snapshot.

Discovery provenance, official URL history, search attempts, and unverified
candidates are saved in the refresh state. Public search requires no API key,
but providers may rate-limit or change their result pages. Such failures are
recorded and retried by the next daily check; existing verified data is retained.
Search snippets and self-described official sites are never used as sole proof.

Run the offline parser regression tests with:

```bash
python3 -m unittest discover -s tests -v
```

Deadlines and rankings are provided for convenience. Always verify submission details on the official venue website before submitting.
