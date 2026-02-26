# TODO

Items marked [DESIGN] need a dedicated brainstorming/planning session before implementation.

## High priority

- [ ] **Crash recovery / checkpointing** -- Save progress after each gene/topic so a mid-run crash doesn't lose already-uploaded data. Resume from last checkpoint on next run.

- [ ] **DOI dedup** -- Add DOI as secondary dedup key alongside PMID. Catches preprints, European journals, and records missing PMIDs that currently slip through.

- [ ] **Inverse bot: flag low-value library papers** -- [DESIGN] For each existing library paper, compute how well it would score if discovered today. Flag the bottom percentile for manual review/removal. Helps keep the library lean as it grows.

- [ ] **Citation graph visualization** -- [DESIGN] Render the citation network per gene (seeds + expanded papers + relations) as an interactive graph. Shows how papers connect, where expansion reaches, and which papers are hubs. Could be a dashboard tab or Excalidraw export.

## Medium priority

- [ ] **Dashboard rescue queue** -- One-click "rescue" button on near-miss articles that writes to `data/rescue_queue.json`. Bot picks them up on next run. Consider twice-weekly cron so rescued articles don't wait a full week.

- [ ] **Dashboard: highlight exclusion triggers** -- For text/MeSH-excluded articles, highlight the specific words or MeSH terms that triggered the exclusion. Makes it easy to spot false positives and refine filters.

- [ ] **Dashboard: recent additions panel** -- Companion to near-misses: show papers uploaded in the last 4 weeks, grouped by gene/topic. Gives a complete picture of pipeline activity alongside rejections.

- [ ] **Per-run metrics + run history** -- Append per-run stats to `data/run_history.json`: papers found/uploaded/rejected per gene/topic, error count, duration. Generate a markdown summary in GitHub Actions job summary. Enables trend analysis.

- [ ] **Periodic gene re-search** -- Currently search pass is skipped if gene already has papers (bootstrap-only). Re-run search every N weeks to catch papers that match the query but aren't in the citation network.

- [ ] **Post-upload verification** -- After batch upload, re-fetch collection PMIDs and compare against intended uploads. Catches papers silently lost to Zotero timeouts.

- [ ] **Near-miss semantic clustering** -- [DESIGN] Group near-miss articles by title/abstract similarity (TF-IDF + cosine). If 5 near-misses cluster around a topic not in the library, that's a systematic blind spot, not 5 independent rejections.

- [x] **Category aliases in topics.yml** -- Move hardcoded `--topics anatomy` aliases from run.py into topics.yml as an `alias` field. Self-documenting and user-extensible.

- [ ] **Mention filter: log dropped candidates as near-misses** -- Citation candidates removed by the mention filter are currently invisible. Log them with reason `mention_filter` so the dashboard surfaces them.

## Low priority

- [ ] **Watch list with email digest** -- Add `watch` field in genes.yml/topics.yml for hot items. Weekly run sends email summary of new papers found. GitHub Actions email via action or webhook.

- [ ] **Author network tracking** -- Identify prolific authors (3+ papers in library). Auto-search their recent publications each run.

- [ ] **PubMed as supplementary source for topics** -- Add Entrez API search as secondary source for topic pipeline. Cross-dedup with OpenAlex results.

- [ ] **Auto-regenerate genes.yml in CI** -- GitHub Action pre-step (or vault push trigger) runs `build_genes_yml.py` so the bot always uses the latest gene list without manual rebuild.

- [ ] **MeSH terms as Zotero tags** -- Write top MeSH descriptors from OpenAlex as Zotero tags on uploaded papers. Enriches library browsing.

- [ ] **Stale gene detection** -- Flag genes with no new papers found in last N runs. Either well-covered (good) or search/expansion isn't reaching new literature (investigate).

- [x] **HGNC alias overrides** -- Per-gene alias blocklist in genes.yml for genes whose HGNC aliases are common words or collide with other symbols.

- [x] **Robust PMID parsing from Zotero extra field** -- Current parser expects `PMID:` prefix or PubMed URL. Add case-insensitive matching and alternative formats to prevent phantom duplicates.

- [ ] **Force-include list** -- [DESIGN] `include_pmids` field in genes.yml for manually discovered papers. Bot handles tagging, collection placement, and relation linking.

- [ ] **Cross-gene pathway boosting** -- [DESIGN] Boost priority when a paper mentions multiple genes from the list. Needs scoring formula integration design.

- [ ] **Gene discovery / candidate suggestions** -- [DESIGN] Suggest new genes based on co-occurrence, shared pathways, or citation overlap with existing library.

- [ ] **Reading queue prioritization** -- [DESIGN] Score unread Zotero items by recency, citation velocity, gene mentions, review vs primary. Output ranked "read next" list.

- [ ] **Literature velocity metric** -- [DESIGN] Track new papers per gene per month across runs. Flag accelerating genes (hot area) and decelerating ones (mature/stalled).

- [x] **Mention filter fallback on empty results** -- If mention filter removes all citation candidates for a gene, raise a GitHub issue rather than silently returning zero. Guards against empty-library edge case.

- [ ] **Cache backward references** -- Currently backward references (referenced_works) are re-fetched fresh every run even if unchanged. Cache the reference list per seed to save API calls on stable papers.

- [ ] **Split workflow into two jobs to bypass 360-min limit** -- Gene pipeline and topic pipeline run as separate GitHub Actions jobs (each gets its own 360-min timeout). Share near-miss data and citation cache via artifacts. Currently the combined run takes 5+ hours, hitting the per-job ceiling.

- [ ] **Spread runs across multiple days for OpenAlex budget** -- Instead of one weekly mega-run, split genes and topics across different days (e.g. genes Mon/Wed, topics Tue/Thu, or rotate gene batches daily). Stays within OpenAlex daily API limits and reduces per-run duration. Requires checkpointing and cumulative near-miss merging across runs.
