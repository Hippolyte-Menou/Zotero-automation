# TODO

## High priority

- [ ] **Crash recovery / checkpointing** -- Save progress after each gene/topic so a mid-run crash (API timeout, rate limit) doesn't lose all data. Resume from last checkpoint on next run. Plan in a dedicated session.

- [ ] **Dashboard rescue queue** -- One-click "rescue" button on near-miss articles that writes to `data/rescue_queue.json`. Next bot run picks them up and uploads to Zotero. Consider a more frequent cron job (e.g., twice-weekly) so rescued articles don't wait a full week.

- [ ] **DOI dedup** -- Currently dedup is PMID-only. Add DOI as a secondary dedup key to catch preprints, alternate records, and entries missing PMIDs.

## Medium priority

- [ ] **Watch list with email digest** -- Add a `watch` field in `genes.yml` and `topics.yml` to flag hot genes/topics. Weekly run sends an email summary of new papers found for watched items. GitHub Actions can send email via a simple action or webhook.

- [ ] **Author network tracking** -- Identify prolific authors (appearing 3+ times in the Zotero library). Auto-search their recent publications on each run. Authors tend to work in coherent threads -- if 5 papers by someone are in the library, their 6th is likely relevant.

- [ ] **PubMed as supplementary source for topics** -- OpenAlex misses some papers, especially for MeSH-based queries. Add PubMed search (via Entrez API) as a secondary source for the topic pipeline. Cross-dedup with OpenAlex results.

## Low priority / needs design

- [ ] **Cross-gene pathway boosting** -- When a paper mentions multiple genes from the gene list, boost its priority score. Papers connecting two "known" genes are disproportionately valuable -- they reveal pathway-level relationships. Needs design for scoring formula integration.

- [ ] **Gene discovery / candidate suggestions** -- Beyond searching known genes, suggest new genes to add based on co-occurrence patterns, shared pathways, or citation overlap with the existing library. Needs a longer design session.

- [ ] **Reading queue prioritization** -- Score unread Zotero items by: recency, citation velocity (citations/year), number of library genes mentioned, review vs primary data. Output a ranked "read next" list. Needs design for where the output lives (dashboard? vault markdown?).
