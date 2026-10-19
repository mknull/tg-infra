# Publisher contracts

Synthetic, sanitized examples of structures observed in public feeds on 2026-10-18.
Names, vacancy text, IDs and job links are replaced; these are not live vacancies.
Official discovery URLs and checked options are in `lib/rss_catalog.json`.

- FOSSJobs: URL identity without a GUID, non-RFC publication date, HTML description.
- FSF: RSS 1.0, HTTP job identities, update-only dates, mixed information/volunteer/jobs.
- Open Source JobHub: GUID, full HTML description; tested search parameters are ignored.
- WWR: regional variants share a GUID; preserve both descriptions and trailing entities.
- NoDesk: invalid XML HTML entities, short summaries, full JobPosting JSON-LD on pages.
- Tails: a legitimate empty feed with channel metadata.

Detail-page fixtures retain only the verified extraction boundaries (`content-core`
for FSF; JobPosting JSON-LD for NoDesk).
