# TODO

Items marked [DESIGN] need a dedicated brainstorming/planning session before implementation.

## High priority

- [x] **Crash recovery / checkpointing** -- Save progress after each gene/topic so a mid-run crash doesn't lose already-uploaded data. Resume from last checkpoint on next run. *(Implemented: `_flush_incremental_state()` saves citation cache + rejection log + recent additions + `data/checkpoint.json` after each gene/topic; on restart, completed items are skipped; checkpoint ignored for targeted runs and discarded if > 48h old; workflow fetches/deploys checkpoint from gh-pages.)*

- [x] **DOI dedup** -- Add DOI as secondary dedup key alongside PMID. Catches preprints, European journals, and records missing PMIDs that currently slip through.

- [x] **Orphan paper detection (Inverse bot)** -- Compute internal citation graph centrality for all Zotero library papers. Flag orphans (centrality = 0, no backward/forward/bib-coupling links to any other library paper) for manual review. *(Implemented: `inverse_bot.py` builds adjacency from backward refs + forward citations + bib coupling, scores every paper, flags zero-centrality orphans; output `site/data/flagged_papers.json` with `hierarchy` dict + `category`/`subcollection` fields; whitelist in `data/inverse_bot_whitelist.json`; Sunday cron `inverse_bot.yml`; dashboard Review tab with dismiss + whitelist download.)*

- [x] **Review tab: same sidebar architecture as Near Misses** -- Review tab sidebar was a flat list of subcollection names derived from a `subcollections` array. Aligned it with the Near Misses two-tier collapsible hierarchy (category -> subcollection, A-Z strip for Genes, "Shared Flagged" top entry). *(Implemented: `zotero_client.get_all_items_full()` now resolves `category` and `subcollection` string fields via parent-chain walk; `inverse_bot.save_flagged_papers()` builds `hierarchy` dict; `generate_test_data.py` updated to match; `renderReviewSidebar()` rewritten to mirror `renderSidebar()`; `getReviewArticles()` uses comma-split filtering identical to Near Misses.)*

- [x] **Inverse bot: flag low-value library papers** -- [DESIGN] For each existing library paper, compute how well it would score if discovered today. Flag the bottom percentile for manual review/removal. Helps keep the library lean as it grows.

- [ ] **Citation graph visualization** -- [DESIGN] Render the citation network per gene (seeds + expanded papers + relations) as an interactive graph. Shows how papers connect, where expansion reaches, and which papers are hubs. Could be a dashboard tab or Excalidraw export.

## Medium priority

- [x] **Dashboard rescue queue** -- One-click "rescue" button on near-miss articles that writes to `data/rescue_queue.json`. Bot picks them up on next run. Consider twice-weekly cron so rescued articles don't wait a full week. *(Implemented: dashboard stores rescued articles in localStorage with per-article Rescue/Queued toggle; "Download rescue_queue.json" exports the queue; run.py `process_rescue_queue()` looks up articles on OpenAlex and uploads them to the target Zotero collection with `source:rescue` tag; queue file is cleared after successful processing.)*

- [x] **Dashboard: highlight exclusion triggers** -- For text/MeSH-excluded articles, highlight the specific words or MeSH terms that triggered the exclusion. Makes it easy to spot false positives and refine filters. *(Implemented: `highlightTerm()` wraps matched text-exclusion terms in `<span class="trigger-mark">` in both title and abstract; MeSH-excluded articles show the triggering descriptor as a styled tag badge. Dark mode supported.)*

- [x] **Dashboard: recent additions panel** -- Companion to near-misses: show papers uploaded in the last 4 weeks, grouped by gene/topic. Gives a complete picture of pipeline activity alongside rejections. *(Implemented: `_track_additions()` records each uploaded paper with PMID, title, year, subcollection, category, source tag, and upload timestamp; `save_recent_additions()` merges with previous data and prunes entries older than 8 weeks; dashboard loads `data/recent_additions.json` and renders a collapsible "Recent" panel grouped by subcollection with source-tag badges and PubMed links.)*

- [x] **Per-run metrics + run history** -- Append per-run stats to `data/run_history.json`: papers found/uploaded/rejected per gene/topic, error count, duration. Generate a markdown summary in GitHub Actions job summary. Enables trend analysis. *(Implemented: `build_run_record()` / `save_run_history()` in run.py append cumulative records; `write_github_summary()` outputs markdown table to `$GITHUB_STEP_SUMMARY`; workflow fetches/deploys `run_history.json` alongside other data.)*

- [x] **Periodic gene re-search** -- Currently search pass is skipped if gene already has papers (bootstrap-only). Re-run search every N weeks to catch papers that match the query but aren't in the citation network. *(Implemented via `re_search_interval_weeks` in genes.yml search config; tracks `last_search_date` per gene in citation cache.)*

- [x] **Post-upload verification** -- After batch upload, re-fetch collection PMIDs and compare against intended uploads. Catches papers silently lost to Zotero timeouts.

- [ ] **Near-miss semantic clustering** -- [DESIGN] Group near-miss articles by title/abstract similarity (TF-IDF + cosine). If 5 near-misses cluster around a topic not in the library, that's a systematic blind spot, not 5 independent rejections.

- [x] **Category aliases in topics.yml** -- Move hardcoded `--topics anatomy` aliases from run.py into topics.yml as an `alias` field. Self-documenting and user-extensible.

- [x] **Mention filter: log dropped candidates as near-misses** -- Citation candidates removed by the mention filter are currently invisible. Log them with reason `mention_filter` so the dashboard surfaces them. *(Already implemented in bio_toolkit OpenAlexClient.expand_citations.)*

## Low priority

- [ ] **Watch list with email digest** -- Add `watch` field in genes.yml/topics.yml for hot items. Weekly run sends email summary of new papers found. GitHub Actions email via action or webhook.

- [ ] **Author network tracking** -- Identify prolific authors (3+ papers in library). Auto-search their recent publications each run.

- [ ] **PubMed as supplementary source for topics** -- Add Entrez API search as secondary source for topic pipeline. Cross-dedup with OpenAlex results.

- [ ] **Auto-regenerate genes.yml in CI** -- GitHub Action pre-step (or vault push trigger) runs the vault-side `build_genes_yml.py` (in `Ophtalmogenetics/_assets/code/gene-generation/`) so the bot always uses the latest gene list without manual rebuild. Note: this needs vault access from CI, which the bot's workflows don't currently have.

- [x] **MeSH terms as Zotero tags** -- Write top MeSH descriptors from OpenAlex as Zotero tags on uploaded papers. Enriches library browsing.

- [x] **Stale gene detection** -- Flag genes with no new papers found in last N runs. Either well-covered (good) or search/expansion isn't reaching new literature (investigate). *(Single-run warning implemented; multi-run tracking across N runs requires per-run metrics.)*

- [x] **HGNC alias overrides** -- Per-gene alias blocklist in genes.yml for genes whose HGNC aliases are common words or collide with other symbols.

- [x] **Robust PMID parsing from Zotero extra field** -- Current parser expects `PMID:` prefix or PubMed URL. Add case-insensitive matching and alternative formats to prevent phantom duplicates.

- [ ] **Force-include list** -- [DESIGN] `include_pmids` field in genes.yml for manually discovered papers. Bot handles tagging, collection placement, and relation linking.

- [ ] **Cross-gene pathway boosting** -- [DESIGN] Boost priority when a paper mentions multiple genes from the list. Needs scoring formula integration design.

- [ ] **Gene discovery / candidate suggestions** -- [DESIGN] Suggest new genes based on co-occurrence, shared pathways, or citation overlap with existing library.

- [ ] **Reading queue prioritization** -- [DESIGN] Score unread Zotero items by recency, citation velocity, gene mentions, review vs primary. Output ranked "read next" list.

- [ ] **Literature velocity metric** -- [DESIGN] Track new papers per gene per month across runs. Flag accelerating genes (hot area) and decelerating ones (mature/stalled).

- [x] **Mention filter fallback on empty results** -- If mention filter removes all citation candidates for a gene, raise a GitHub issue rather than silently returning zero. Guards against empty-library edge case.

- [x] **Cache backward references** -- Currently backward references (referenced_works) are re-fetched fresh every run even if unchanged. Cache the reference list per seed to save API calls on stable papers. *(Implemented: `_expand_one_hop` stores each seed's `referenced_works` in the citation cache and uses cached refs as fallback; `expand_citations` bib-coupling seed ref set also uses cache fallback.)*

- [x] **Split workflow into separate jobs/workflows** -- Gene pipeline and topic pipeline run independently with their own timeouts. *(Originally: 3 sequential jobs in one workflow. Now: separate workflows -- `gene_pipeline.yml` Mon-Fri, `topic_pipeline.yml` Sat, each with 2 jobs: run + deploy. Deploy uses `keep_files: true` so daily deploys never wipe other days' data.)*

- [x] **Spread runs across multiple days for OpenAlex budget** -- Instead of one weekly mega-run, split genes and topics across different days. *(Implemented: `gene_pipeline.yml` runs Mon-Fri 06:00 UTC with 1/5 rotation (38 genes get full expansion per day, all 189 covered weekly); `topic_pipeline.yml` runs Saturday; `inverse_bot.yml` moved to Sunday. Old combined `genebot.yml` deleted. `select_rotation_genes()` changed from `// 4` to `// 5`.)*

- [x] **Dashboard: run history panel** -- Load `data/run_history.json` in the near-miss dashboard and display per-run stats (papers found/uploaded/failed per gene/topic, OpenAlex errors). *(Implemented: collapsible "History" panel in meta bar loads run_history.json and renders a table with date, pipelines, found/new/uploaded/failed counts, and API errors per run.)*
