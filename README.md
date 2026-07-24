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
- discovers target-year links from official hubs and their sitemaps;
- accepts an edition page only after HTTP success and a venue + year identity check;
- extracts high-confidence locations from Schema.org data, labelled official text,
  event banners, or explicit official future-meetings pages;
- keeps the last verified value when a site is temporarily unavailable; and
- requires two consecutive observations before replacing an existing location.

GitHub Actions runs this refresh every six hours. The site remains a static,
fast snapshot and reloads that snapshot every six hours while left open. An
unannounced edition links to its official series hub instead of a guessed or
broken future URL.

Each accepted location stores its official source URL, extraction method,
evidence text, precision, and verification time in `data/refresh_state.json`.
The generated `data/conferences.js` contains only the display snapshot.

Run the parser tests with:

```bash
python3 -m unittest discover -s tests -v
```

Deadlines and rankings are provided for convenience. Always verify submission details on the official venue website before submitting.
