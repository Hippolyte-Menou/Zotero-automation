# Dashboard TODO

## High priority

- [x] **Sortable columns** -- Clickable column headers (Article, Reason, Score, Cited, Year) with toggle asc/desc and visual sort arrows. Score column shows effective_score/threshold with breakdown. Dropdown kept for recurring/first-seen sorts.

- [x] **Cumulative mode** -- Merge new rejections into existing JSON instead of overwriting. Deduplicate by PMID, add `first_seen` / `last_seen` / `seen_count` fields. Papers that recur across runs are strong rescue candidates.

- [x] **Threshold tuning panel** -- Sidebar widget to temporarily adjust the adaptive threshold and preview which articles would have passed. Helps calibrate `min_co_citations` and `max_min_co` without re-running the pipeline.

## Medium priority

- [x] **Cross-subcollection duplicates** -- Highlight articles rejected in multiple genes/topics. Same PMID appearing for RPGR and CRB1 signals broad relevance. Add a "shared near-misses" grouped view.

- [x] **Summary statistics panel** -- Collapsible stats bar at the top: total rejections, breakdown by reason (horizontal bars), top 5 subcollections by count. Toggle via "Stats" button in meta bar.

- [x] **"How close" indicator** -- For score-below-threshold articles, visual progress bar showing `effective_score / threshold` ratio with red/amber/green gradient. "Closest to threshold" sort preset auto-filters to score articles.

## Low priority

- [x] **Dark mode** -- Add `prefers-color-scheme: dark` media query with inverted palette. CSS is already structured for this.
