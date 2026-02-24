# Dashboard TODO

## High priority

- [ ] **Sortable columns** -- Add clickable column headers to sort by cited-by count, effective score, year, journal. Score-below-threshold articles already have all fields; text/MeSH exclusions sort by cited-by and year.

- [ ] **Cumulative mode** -- Merge new rejections into existing JSON instead of overwriting. Deduplicate by PMID, add `first_seen` / `last_seen` / `seen_count` fields. Papers that recur across runs are strong rescue candidates.

- [ ] **Threshold tuning panel** -- Sidebar widget to temporarily adjust the adaptive threshold and preview which articles would have passed. Helps calibrate `min_co_citations` and `max_min_co` without re-running the pipeline.

## Medium priority

- [ ] **Cross-subcollection duplicates** -- Highlight articles rejected in multiple genes/topics. Same PMID appearing for RPGR and CRB1 signals broad relevance. Add a "shared near-misses" grouped view.

- [ ] **Summary statistics panel** -- Collapsible stats bar at the top: total rejections, breakdown by reason (pie/bar), top 5 subcollections by count. Quick overview without scrolling.

- [ ] **"How close" indicator** -- For score-below-threshold articles, show `effective_score / threshold` as a visual progress bar. Add a "closest misses" preset sort to surface 5/6 before 1/6.

## Low priority

- [ ] **Dark mode** -- Add `prefers-color-scheme: dark` media query with inverted palette. CSS is already structured for this.
