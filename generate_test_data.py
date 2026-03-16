#!/usr/bin/env python3
"""Generate realistic synthetic data for dashboard testing.

Run from repo root:
    python generate_test_data.py

Outputs:
    site/data/near_misses.json       -- rejected articles with cumulative tracking
    site/data/run_history.json       -- per-run metrics (History panel)
    site/data/recent_additions.json  -- recently uploaded papers (Recent panel)
    site/data/rescue_queue.json      -- articles queued for rescue
"""

import json
import random
import os
from datetime import datetime, timezone, timedelta

random.seed(42)

# -- Realistic genes from genes.yml subset --
GENES = [
    "ABCA4", "ACO2", "ADAM9", "ADGRV1", "AHI1", "AIPL1", "ALMS1",
    "ARL6", "BBS1", "BBS2", "BBS10", "BEST1", "C2orf71", "CACNA1F",
    "CDH23", "CEP290", "CHM", "CLN3", "CNGA3", "CNGB3", "CRB1",
    "CRX", "CYP4V2", "EYS", "FAM161A", "GUCY2D", "IMPDH1", "INPP5E",
    "IQCB1", "KCNV2", "KIF11", "LRAT", "MAK", "MERTK", "MYO7A",
    "NR2E3", "NRL", "OPA1", "OPA3", "PAX6", "PDE6A", "PDE6B",
    "PDE6C", "PROM1", "PRPF31", "PRPF8", "PRPH2", "RDH12", "RHO",
    "RP1", "RP2", "RPE65", "RPGR", "RS1", "SAG", "SNRNP200",
    "TOPORS", "TRIM32", "TULP1", "USH2A",
]

TOPIC_HIERARCHY = {
    "1 - Anatomy": [
        "Outer coat", "Inner coat", "Middle coat",
        "Transparent media", "Adnexal structures",
        "Vascularization and innervation", "Orbit", "Specialized microanatomy",
    ],
    "2 - Embryology": [
        "Developmental timeline", "Developmental genetics",
        "Optic vesicle", "Coat differentiation",
        "Derived structures", "Adnexal development", "Ocular malformations",
    ],
    "3 - Physiology": [
        "Visual physiology", "Transparent media physiology",
        "Pupillary physiology", "Ocular motility", "Intraocular pressure",
        "Ocular vascular physiology", "Ocular neurophysiology",
    ],
    "4 - Examinations": [
        "Visual function", "Anterior segment examination",
        "Posterior segment examination", "Ocular imaging",
        "Specialized functional testing", "Genetic testing",
    ],
    "5 - Pathologies": [
        "Retinal dystrophies", "Corneal dystrophies", "Congenital cataracts",
        "Albinism", "Ocular developmental anomalies", "Vitreoretinopathies",
    ],
}

JOURNALS = [
    "Investigative Ophthalmology & Visual Science",
    "Ophthalmology",
    "JAMA Ophthalmology",
    "British Journal of Ophthalmology",
    "American Journal of Ophthalmology",
    "Retina",
    "Molecular Vision",
    "Human Molecular Genetics",
    "Human Mutation",
    "European Journal of Human Genetics",
    "Genetics in Medicine",
    "Progress in Retinal and Eye Research",
    "Experimental Eye Research",
    "Acta Ophthalmologica",
    "Ophthalmic Genetics",
    "Nature Genetics",
    "Molecular Therapy",
    "Gene Therapy",
    "Clinical Genetics",
    "Journal of Medical Genetics",
    "Graefe's Archive for Clinical and Experimental Ophthalmology",
    "Documenta Ophthalmologica",
    "Vision Research",
    "Current Opinion in Ophthalmology",
    "Survey of Ophthalmology",
]

FIRST_NAMES = [
    "James", "Sarah", "Mohammed", "Yuki", "Marie", "David", "Amina",
    "Robert", "Lisa", "Chen", "Anna", "Klaus", "Sofia", "Pablo",
    "Hiroshi", "Rachel", "Thomas", "Emily", "Jean-Pierre", "Fatima",
    "Alessandro", "Priya", "Hans", "Mei", "Pierre", "Youssef",
    "Ingrid", "Marco", "Aiko", "Philippe",
]
LAST_NAMES = [
    "Smith", "Khan", "Tanaka", "Dupont", "Chen", "Mueller", "Garcia",
    "Johnson", "Wang", "Schmidt", "Rodriguez", "Martin", "Wilson",
    "Lee", "Brown", "Davis", "Sato", "Weber", "Ali", "Park",
    "Rossi", "Sharma", "Johansson", "Liu", "Dubois", "Hassan",
    "Bergman", "Moretti", "Nakamura", "Lefevre",
]

# -- Gene aliases (subset for test data) --
GENE_ALIASES = {
    "ABCA4": ["ABCR", "STGD1", "FFM", "RP19"],
    "RPGR": ["RP3", "XLRP3", "COD1", "RP15"],
    "CRB1": ["RP12", "LCA8"],
    "RHO": ["RP4", "OPN2", "rhodopsin"],
    "USH2A": ["USH2", "RP39", "usherin"],
    "RPE65": ["LCA2", "RP20", "retinal pigment epithelium 65"],
    "PAX6": ["AN2", "aniridia"],
    "CEP290": ["BBS14", "JBTS5", "LCA10", "NPHP6"],
    "RS1": ["XLRS1", "retinoschisin"],
    "BEST1": ["VMD2", "bestrophin-1"],
}

# -- Subtopic keyword samples (for topic articles) --
SUBTOPIC_KEYWORDS = {
    "Outer coat": ["sclera", "cornea", "limbus"],
    "Inner coat": ["retina", "photoreceptor", "ganglion cell"],
    "Middle coat": ["choroid", "ciliary body", "iris"],
    "Transparent media": ["lens", "vitreous", "aqueous humor"],
    "Developmental timeline": ["optic vesicle", "eye morphogenesis"],
    "Developmental genetics": ["PAX6", "SOX2", "SHH", "hedgehog"],
    "Visual physiology": ["phototransduction", "visual cycle", "dark adaptation"],
    "Ocular imaging": ["OCT", "fundus autofluorescence", "adaptive optics"],
    "Retinal dystrophies": ["retinitis pigmentosa", "cone-rod dystrophy", "Stargardt"],
    "Corneal dystrophies": ["Fuchs dystrophy", "keratoconus", "TGFBI"],
}

TEXT_EXCLUSION_TERMS = [
    "cancer", "tumor", "neoplasm", "carcinoma", "melanoma",
    "metastasis", "chemotherapy", "mouse model", "zebrafish",
    "drosophila", "c. elegans",
]

MESH_EXCLUSION_TERMS = [
    "Neoplasms", "Diabetes Mellitus", "Diabetes Mellitus, Type 2",
    "Pancreatic Neoplasms", "Breast Neoplasms", "Alzheimer Disease",
    "Parkinson Disease", "Multiple Sclerosis",
]

# -- Title templates --
GENE_TITLE_TEMPLATES = [
    "Novel {gene} variants in {disease} families from {region}",
    "Deep phenotyping of {gene}-associated retinal dystrophies using multimodal imaging",
    "{gene} gene therapy: preclinical efficacy in {model}",
    "Genotype-phenotype correlations in {gene}-related {disease}",
    "Long-term natural history of {gene}-associated {disease}",
    "Structural and functional outcomes in patients with {gene} mutations",
    "Expanding the phenotypic spectrum of {gene}-related retinal disease",
    "A novel splice-site variant in {gene} causing {disease}",
    "{gene} interactome mapping reveals novel ciliary transport mechanisms",
    "Antisense oligonucleotide therapy for {gene}-associated retinitis pigmentosa",
    "Optical coherence tomography findings in {gene} retinal dystrophy",
    "Electrophysiological characterization of {gene}-deficient photoreceptors",
    "CRISPR-based correction of {gene} mutations in patient-derived iPSCs",
    "Population-based prevalence of {gene} variants in inherited retinal disease",
    "Modifier genes influencing {gene}-associated retinal degeneration severity",
]

TOPIC_TITLE_TEMPLATES = [
    "Corneal biomechanics and {condition}: a systematic review",
    "Retinal ganglion cell layer thickness as a biomarker in {condition}",
    "{structure} development and congenital anomalies: current concepts",
    "Advances in {technique} for inherited retinal diseases",
    "The role of {process} in retinal homeostasis and disease",
    "Clinical utility of {technique} in diagnosing {condition}",
    "Embryological basis of {condition}: insights from genetic models",
    "Comparative anatomy of the {structure} across vertebrate species",
    "Physiological mechanisms underlying {process} in the retina",
    "Novel biomarkers from {technique} in pediatric ophthalmology",
]

DISEASES = [
    "retinitis pigmentosa", "Stargardt disease", "Leber congenital amaurosis",
    "cone-rod dystrophy", "Usher syndrome", "Bardet-Biedl syndrome",
    "achromatopsia", "congenital stationary night blindness",
    "Best disease", "choroideremia", "X-linked retinoschisis",
    "aniridia", "anterior segment dysgenesis",
]

REGIONS = [
    "South Asia", "Northern Europe", "East Asia", "Middle East",
    "Sub-Saharan Africa", "Latin America", "Mediterranean",
]

MODELS = [
    "a mouse model", "patient-derived organoids", "iPSC-derived retinal cells",
    "a canine model", "retinal explants",
]

CONDITIONS = [
    "inherited retinal dystrophy", "glaucoma", "macular degeneration",
    "optic neuropathy", "corneal dystrophy", "congenital cataract",
    "albinism", "diabetic retinopathy", "uveitis",
]

STRUCTURES = [
    "retinal pigment epithelium", "photoreceptor outer segment",
    "inner nuclear layer", "corneal endothelium", "lens capsule",
    "optic nerve head", "choroid",
]

TECHNIQUES = [
    "OCT angiography", "adaptive optics imaging", "multifocal ERG",
    "fundus autofluorescence", "ultra-widefield imaging",
]

PROCESSES = [
    "phototransduction", "visual cycle retinoid metabolism",
    "ciliary trafficking", "autophagy", "oxidative stress response",
]

ABSTRACT_TEMPLATES = [
    "We identified {n} novel variants in a cohort of {m} families. Functional assays demonstrated pathogenicity for all variants. Clinical features included progressive visual field loss and reduced ERG responses.",
    "Comprehensive phenotypic characterization of {m} patients using OCT, autofluorescence, and adaptive optics imaging revealed distinct patterns of photoreceptor degeneration.",
    "This systematic review and meta-analysis included {m} studies with a total of {k} participants. Results showed significant associations between the studied markers and disease progression.",
    "We demonstrate that the therapeutic approach restores retinal function with sustained expression for up to {n} months in preclinical testing. Electroretinography showed dose-dependent improvement.",
    "Long-term follow-up of {m} patients over a median of {n} years revealed variable rates of progression. Baseline ellipsoid zone width was the strongest predictor of visual acuity decline.",
    "Proximity labeling proteomics identifies {n} novel binding partners involved in protein trafficking. Several interact with known ciliopathy genes, suggesting shared pathogenic mechanisms.",
    "Analysis of {m} cases from a national registry reveals genotype-phenotype correlations. Truncating variants were associated with earlier onset and more severe disease.",
    "We report the clinical and genetic findings in {m} unrelated families. Segregation analysis and in silico predictions support pathogenicity of the identified variants.",
    "Using single-cell RNA sequencing of {k} retinal cells, we characterize the transcriptomic landscape and identify cell-type-specific expression patterns relevant to disease.",
    "A prospective study of {m} patients evaluating structural and functional outcomes over {n} years. Microperimetry sensitivity declined at a rate of 0.{n}dB/year.",
]

# Sentences to inject into text-excluded articles so the matched term appears in the text.
# The highlight feature only fires when the term is actually present.
TEXT_EXCLUSION_INJECT = {
    "cancer":       "A subset of patients had a prior history of cancer, noted as a potential confounder.",
    "tumor":        "Tumor suppressor pathway activity was assessed as a secondary endpoint.",
    "neoplasm":     "No evidence of neoplasm was observed in any of the study participants.",
    "carcinoma":    "Concurrent carcinoma diagnoses were recorded and excluded from the primary analysis.",
    "melanoma":     "Uveal melanoma was considered in the differential diagnosis before genetic testing.",
    "metastasis":   "Distant metastasis was not detected in any of the enrolled patients.",
    "chemotherapy": "Two patients had received prior chemotherapy for an unrelated condition.",
    "mouse model":  "Validation experiments were conducted in a mouse model of retinal degeneration.",
    "zebrafish":    "Morpholino knockdown in zebrafish confirmed the functional impact of the splice variant.",
    "drosophila":   "Drosophila rescue experiments were used to assess the pathogenicity of missense variants.",
    "c. elegans":   "Ortholog studies in C. elegans provided additional mechanistic insight.",
}

# Title suffixes to inject the matched term for ~40% of text-excluded articles.
TEXT_EXCLUSION_TITLE_SUFFIX = {
    "cancer":       " in a cancer predisposition context",
    "tumor":        " and tumor suppressor pathway interactions",
    "neoplasm":     " excluding neoplasm-related phenocopies",
    "carcinoma":    ": differential diagnosis from carcinoma-associated retinopathy",
    "melanoma":     " differentiating from uveal melanoma",
    "metastasis":   " ruling out metastasis-associated retinal infiltration",
    "chemotherapy": " in patients with prior chemotherapy exposure",
    "mouse model":  " characterized in a mouse model",
    "zebrafish":    " validated in zebrafish",
    "drosophila":   " using Drosophila functional assays",
    "c. elegans":   " with C. elegans ortholog analysis",
}


def random_authors(n=None):
    if n is None:
        n = random.randint(1, 6)
    return [
        [random.choice(FIRST_NAMES), random.choice(LAST_NAMES)]
        for _ in range(n)
    ]


def random_abstract(inject_term=None):
    tmpl = random.choice(ABSTRACT_TEMPLATES)
    text = tmpl.format(
        n=random.randint(2, 12),
        m=random.randint(10, 120),
        k=random.randint(500, 15000),
    )
    if inject_term and inject_term in TEXT_EXCLUSION_INJECT:
        text = text + " " + TEXT_EXCLUSION_INJECT[inject_term]
    return text


def random_cumulative_fields():
    """Generate first_seen, last_seen, seen_count for synthetic data."""
    now = datetime.now(timezone.utc)
    seen_count = random.choices([1, 2, 3, 4, 5], weights=[50, 25, 13, 7, 5], k=1)[0]
    # first_seen is 1-12 weeks ago
    weeks_ago = random.randint(1, 12)
    first_seen = now - timedelta(weeks=weeks_ago)
    if seen_count > 1:
        # last_seen is between first_seen and now
        span = (now - first_seen).total_seconds()
        last_seen = first_seen + timedelta(seconds=random.uniform(span * 0.3, span))
    else:
        last_seen = first_seen
    return {
        "first_seen": first_seen.isoformat(),
        "last_seen": last_seen.isoformat(),
        "seen_count": seen_count,
    }


def _apply_text_exclusion(article, term):
    """Inject matched_term into title (~40% chance) and always into abstract."""
    article["matched_term"] = term
    # Always inject into abstract so the highlight fires there
    article["abstract"] = article["abstract"] + " " + TEXT_EXCLUSION_INJECT.get(term, "")
    # ~40% of text-excluded articles also have the term in the title
    if random.random() < 0.4 and term in TEXT_EXCLUSION_TITLE_SUFFIX:
        article["title"] = article["title"] + TEXT_EXCLUSION_TITLE_SUFFIX[term]


def make_gene_article(gene, reason, pmid_counter):
    pmid = str(38000000 + pmid_counter)
    tmpl = random.choice(GENE_TITLE_TEMPLATES)
    title = tmpl.format(
        gene=gene,
        disease=random.choice(DISEASES),
        region=random.choice(REGIONS),
        model=random.choice(MODELS),
    )

    # Build search_keywords: gene aliases + a disease tag
    aliases = GENE_ALIASES.get(gene, [gene])
    kw = [gene] + aliases
    # Add a disease term for ~50% of articles
    if random.random() < 0.5:
        kw.append(random.choice(DISEASES).replace(" ", "-"))

    article = {
        "pmid": pmid,
        "doi": f"10.1000/synth.{pmid}",
        "title": title,
        "authors": random_authors(),
        "journal": random.choice(JOURNALS),
        "year": str(random.choice([2021, 2022, 2023, 2024, 2025])),
        "abstract": random_abstract(),
        "cited_by_count": random.randint(0, 80),
        "reason": reason,
        "subcollection": gene,
        "category": "6 - Genes",
        "matched_term": None,
        "search_keywords": sorted(set(kw)),
    }

    if reason == "score_below_threshold":
        threshold = random.randint(3, 8)
        co_cit = random.randint(1, threshold - 1)
        bib = random.randint(0, 3)
        recency = random.randint(0, 3)
        eff = co_cit + min(bib, 3) + recency
        if eff >= threshold:
            eff = threshold - 1
        article.update({
            "co_citations": co_cit,
            "bib_coupling": bib,
            "recency_bonus": recency,
            "effective_score": eff,
            "threshold": threshold,
            "direction": random.choice(["citation", "reference", "both"]),
        })
    elif reason == "text_exclusion":
        _apply_text_exclusion(article, random.choice(TEXT_EXCLUSION_TERMS))
    elif reason == "mesh_exclusion":
        article["matched_term"] = random.choice(MESH_EXCLUSION_TERMS)
    elif reason == "mention_filter":
        co_cit = random.randint(2, 8)
        bib = random.randint(0, 4)
        recency = random.randint(0, 3)
        article.update({
            "co_citations": co_cit,
            "bib_coupling": bib,
            "recency_bonus": recency,
            "effective_score": co_cit + min(bib, 3) + recency,
            "direction": random.choice(["citation", "reference", "both"]),
        })

    article.update(random_cumulative_fields())
    return article


def make_topic_article(category, subtopic, reason, pmid_counter):
    pmid = str(38000000 + pmid_counter)
    tmpl = random.choice(TOPIC_TITLE_TEMPLATES)
    title = tmpl.format(
        condition=random.choice(CONDITIONS),
        structure=random.choice(STRUCTURES),
        technique=random.choice(TECHNIQUES),
        process=random.choice(PROCESSES),
    )

    # Build search_keywords from subtopic
    kw = SUBTOPIC_KEYWORDS.get(subtopic, [subtopic.lower()])

    article = {
        "pmid": pmid,
        "doi": f"10.1000/synth.{pmid}",
        "title": title,
        "authors": random_authors(),
        "journal": random.choice(JOURNALS),
        "year": str(random.choice([2021, 2022, 2023, 2024, 2025])),
        "abstract": random_abstract(),
        "cited_by_count": random.randint(0, 60),
        "reason": reason,
        "subcollection": subtopic,
        "category": category,
        "matched_term": None,
        "search_keywords": sorted(set(kw)),
    }

    if reason == "score_below_threshold":
        threshold = random.randint(3, 6)
        co_cit = random.randint(1, threshold - 1)
        bib = random.randint(0, 3)
        recency = random.randint(0, 3)
        eff = co_cit + min(bib, 3) + recency
        if eff >= threshold:
            eff = threshold - 1
        article.update({
            "co_citations": co_cit,
            "bib_coupling": bib,
            "recency_bonus": recency,
            "effective_score": eff,
            "threshold": threshold,
            "direction": random.choice(["citation", "reference", "both"]),
        })
    elif reason == "text_exclusion":
        _apply_text_exclusion(article, random.choice(TEXT_EXCLUSION_TERMS))
    elif reason == "mesh_exclusion":
        article["matched_term"] = random.choice(MESH_EXCLUSION_TERMS)
    elif reason == "mention_filter":
        co_cit = random.randint(2, 6)
        bib = random.randint(0, 3)
        recency = random.randint(0, 3)
        article.update({
            "co_citations": co_cit,
            "bib_coupling": bib,
            "recency_bonus": recency,
            "effective_score": co_cit + min(bib, 3) + recency,
            "direction": random.choice(["citation", "reference", "both"]),
        })

    article.update(random_cumulative_fields())
    return article


def make_run_record(timestamp, run_genes, run_topics):
    """Build a synthetic run record matching run.py build_run_record() output."""
    gene_totals = {"found": 0, "new": 0, "added": 0, "failed": 0,
                   "cit_candidates": 0, "cit_added": 0, "recent_added": 0}
    topic_totals = {"found": 0, "new": 0, "added": 0, "failed": 0,
                    "cit_candidates": 0, "cit_added": 0, "recent_added": 0}
    per_gene = []
    per_topic = []

    if run_genes:
        for gene in random.sample(GENES, random.randint(10, 20)):
            found = random.randint(0, 30)
            new = random.randint(0, found)
            added = random.randint(0, new)
            cit_cand = random.randint(0, 60)
            cit_added = random.randint(0, min(cit_cand, 15))
            recent_added = random.randint(0, 5)
            failed = random.randint(0, max(0, new - added))
            s = {"symbol": gene, "found": found, "new": new, "added": added,
                 "failed": failed, "cit_candidates": cit_cand,
                 "cit_added": cit_added, "recent_added": recent_added}
            per_gene.append(s)
            for k in gene_totals:
                gene_totals[k] += s.get(k, 0)

    if run_topics:
        for cat, subtopics in TOPIC_HIERARCHY.items():
            for sub in random.sample(subtopics, min(len(subtopics), random.randint(2, 4))):
                found = random.randint(5, 40)
                new = random.randint(0, found)
                added = random.randint(0, new)
                cit_cand = random.randint(0, 30)
                cit_added = random.randint(0, min(cit_cand, 10))
                recent_added = random.randint(0, 4)
                failed = random.randint(0, max(0, new - added))
                s = {"name": sub, "found": found, "new": new, "added": added,
                     "failed": failed, "cit_candidates": cit_cand,
                     "cit_added": cit_added, "recent_added": recent_added}
                per_topic.append(s)
                for k in topic_totals:
                    topic_totals[k] += s.get(k, 0)

    return {
        "timestamp": timestamp,
        "pipelines": {"genes": run_genes, "topics": run_topics},
        "genes": {"totals": gene_totals, "per_gene": per_gene},
        "topics": {"totals": topic_totals, "per_topic": per_topic},
        "openalex_failed_requests": random.choices([0, 1, 2, 3], weights=[70, 15, 10, 5], k=1)[0],
    }


def generate_run_history(now):
    """Generate 8 synthetic run history entries over the last 8 weeks."""
    runs = []
    # Alternate between genes-only, topics-only, and both
    patterns = [
        (True, True), (True, False), (True, True), (False, True),
        (True, True), (True, True), (True, False), (True, True),
    ]
    for i, (run_genes, run_topics) in enumerate(patterns):
        ts = now - timedelta(weeks=i)
        timestamp = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        runs.append(make_run_record(timestamp, run_genes, run_topics))
    # Newest first in list so the dashboard shows them in correct order
    runs.reverse()
    return {"runs": runs}


def generate_recent_additions(now, articles):
    """Generate synthetic recent additions from a subset of genes/topics.

    Picks ~80 papers uploaded over the last 6 weeks, using titles from the
    near_misses articles pool (simulates papers that DID get uploaded).
    """
    # Use a separate RNG instance to avoid perturbing the main seed sequence
    rng = random.Random(99)
    sources = ["source:search", "source:citation", "source:recent", "source:rescue"]
    source_weights = [0.4, 0.35, 0.2, 0.05]

    additions = []
    pmid_base = 39000000
    counter = 0

    # Sample a mix of gene and topic subcollections
    gene_subs = [(g, "6 - Genes") for g in rng.sample(GENES, 20)]
    topic_subs = []
    for cat, subs in TOPIC_HIERARCHY.items():
        for sub in rng.sample(subs, min(len(subs), 2)):
            topic_subs.append((sub, cat))

    all_subs = gene_subs + topic_subs

    for sub, cat in all_subs:
        n = rng.randint(1, 6)
        for _ in range(n):
            weeks_ago = rng.uniform(0, 6)
            uploaded_at = (now - timedelta(weeks=weeks_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
            pmid = str(pmid_base + counter)
            counter += 1

            # Pick a title template matching the subcollection type
            if cat == "6 - Genes":
                tmpl = rng.choice(GENE_TITLE_TEMPLATES)
                title = tmpl.format(
                    gene=sub,
                    disease=rng.choice(DISEASES),
                    region=rng.choice(REGIONS),
                    model=rng.choice(MODELS),
                )
            else:
                tmpl = rng.choice(TOPIC_TITLE_TEMPLATES)
                title = tmpl.format(
                    condition=rng.choice(CONDITIONS),
                    structure=rng.choice(STRUCTURES),
                    technique=rng.choice(TECHNIQUES),
                    process=rng.choice(PROCESSES),
                )

            source = rng.choices(sources, weights=source_weights, k=1)[0]
            additions.append({
                "pmid": pmid,
                "doi": f"10.1000/added.{pmid}",
                "title": title,
                "year": str(rng.choice([2023, 2024, 2025])),
                "subcollection": sub,
                "category": cat,
                "source": source,
                "uploaded_at": uploaded_at,
            })

    additions.sort(key=lambda a: a["uploaded_at"], reverse=True)
    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "additions": additions,
    }


def generate_flagged_papers(now):
    """Generate synthetic flagged_papers.json for inverse bot dashboard testing.

    Simulates ~60 orphan papers scattered across gene and topic subcollections.
    """
    rng = random.Random(55)
    source_tags = ["source:search", "source:citation", "source:recent"]
    source_weights = [0.4, 0.45, 0.15]

    articles = []
    pmid_base = 50000000

    all_subs = []
    for gene in rng.sample(GENES, 30):
        all_subs.append((gene, "6 - Genes"))
    for cat, subs in TOPIC_HIERARCHY.items():
        for sub in rng.sample(subs, min(len(subs), 2)):
            all_subs.append((sub, cat))

    for i, (sub, cat) in enumerate(all_subs):
        n = rng.randint(1, 4)
        for j in range(n):
            pmid = str(pmid_base + i * 10 + j)
            source = rng.choices(source_tags, weights=source_weights, k=1)[0]
            # ~30% of flagged papers appear in 2 subcollections (comma-joined strings)
            subcollection = sub
            category = cat
            if rng.random() < 0.3 and len(all_subs) > 1:
                other_sub, other_cat = rng.choice(all_subs)
                if other_sub != sub:
                    subcollection = f"{sub}, {other_sub}"
                    if other_cat != cat:
                        category = f"{cat}, {other_cat}"
            articles.append({
                "pmid": pmid,
                "doi": f"10.9999/orphan.{pmid}",
                "zotero_key": f"ZK{pmid}",
                "title": rng.choice(GENE_TITLE_TEMPLATES).format(
                    gene=sub if cat == "6 - Genes" else rng.choice(GENES),
                    disease=rng.choice(DISEASES),
                    region=rng.choice(REGIONS),
                    model=rng.choice(MODELS),
                ),
                "authors": [
                    [rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)]
                    for _ in range(rng.randint(1, 4))
                ],
                "journal": rng.choice(JOURNALS),
                "year": str(rng.choice([2018, 2019, 2020, 2021, 2022])),
                "abstract": random_abstract(),
                "cited_by_count": rng.randint(0, 8),
                "centrality": 0,
                "source_tags": [source],
                "category": category,
                "subcollection": subcollection,
            })

    articles.sort(key=lambda a: a["title"].lower())

    # Build hierarchy dict (same format as near_misses.json)
    hierarchy: dict = {}
    for a in articles:
        cats = [c.strip() for c in a["category"].split(",") if c.strip()]
        subs = [s.strip() for s in a["subcollection"].split(",") if s.strip()]
        for c, s in zip(cats, subs):
            hierarchy.setdefault(c, set()).add(s)
    hierarchy = {k: sorted(v) for k, v in sorted(hierarchy.items())}

    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "threshold_k": 0,
        "total_evaluated": len(articles) + rng.randint(400, 800),
        "total_flagged": len(articles),
        "total_dismissed": 0,
        "total_unresolvable": rng.randint(5, 20),
        "hierarchy": hierarchy,
        "articles": articles,
    }


def generate_rescue_queue(articles):
    """Generate a rescue queue with 5 near-miss articles."""
    rng = random.Random(77)
    score_articles = [a for a in articles if a["reason"] == "score_below_threshold"]
    picks = rng.sample(score_articles, min(5, len(score_articles)))
    return [
        {
            "pmid": a["pmid"],
            "doi": a.get("doi", ""),
            "title": a["title"],
            "subcollection": a["subcollection"].split(",")[0].strip(),
            "category": a["category"].split(",")[0].strip(),
        }
        for a in picks
    ]


def main():
    now = datetime.now(timezone.utc)
    articles = []
    pmid_counter = 1
    reasons = ["score_below_threshold", "text_exclusion", "mesh_exclusion", "mention_filter"]
    reason_weights = [0.45, 0.25, 0.15, 0.15]

    # Gene articles: 3-15 per gene, ~60 genes -> ~500 articles
    for gene in GENES:
        n_articles = random.randint(3, 15)
        for _ in range(n_articles):
            reason = random.choices(reasons, weights=reason_weights, k=1)[0]
            articles.append(make_gene_article(gene, reason, pmid_counter))
            pmid_counter += 1

    # Topic articles: 5-20 per subtopic -> ~300 articles
    for category, subtopics in TOPIC_HIERARCHY.items():
        for subtopic in subtopics:
            n_articles = random.randint(5, 20)
            for _ in range(n_articles):
                reason = random.choices(reasons, weights=reason_weights, k=1)[0]
                articles.append(
                    make_topic_article(category, subtopic, reason, pmid_counter)
                )
                pmid_counter += 1

    # Cross-subcollection shared articles (~30 entries)
    # These simulate articles rejected in multiple subcollections simultaneously
    all_topic_subs = []
    for cat, subs in TOPIC_HIERARCHY.items():
        for sub in subs:
            all_topic_subs.append((cat, sub))

    shared_count = 0
    for _ in range(30):
        combo_type = random.choice(["gene-gene", "gene-topic", "topic-topic"])
        reason = random.choices(reasons, weights=reason_weights, k=1)[0]

        if combo_type == "gene-gene":
            genes = random.sample(GENES, random.randint(2, 3))
            subcollections = ",".join(genes)
            categories = ",".join(["6 - Genes"] * len(genes))
            article = make_gene_article(genes[0], reason, pmid_counter)
        elif combo_type == "gene-topic":
            gene = random.choice(GENES)
            topic_cat, topic_sub = random.choice(all_topic_subs)
            subcollections = f"{gene},{topic_sub}"
            categories = f"6 - Genes,{topic_cat}"
            article = make_gene_article(gene, reason, pmid_counter)
        else:  # topic-topic
            picks = random.sample(all_topic_subs, random.randint(2, 3))
            subcollections = ",".join(p[1] for p in picks)
            categories = ",".join(p[0] for p in picks)
            article = make_topic_article(picks[0][0], picks[0][1], reason, pmid_counter)

        article["subcollection"] = subcollections
        article["category"] = categories
        # Higher seen_count for shared articles (stronger recurring signal)
        article["seen_count"] = random.randint(2, 6)
        articles.append(article)
        pmid_counter += 1
        shared_count += 1

    # Build hierarchy
    hierarchy = {"6 - Genes": sorted(GENES)}
    for cat, subs in sorted(TOPIC_HIERARCHY.items()):
        hierarchy[cat] = subs

    # Build stats
    by_reason = {}
    by_category = {}
    for a in articles:
        by_reason[a["reason"]] = by_reason.get(a["reason"], 0) + 1
        by_category[a["category"]] = by_category.get(a["category"], 0) + 1

    pipeline_runs = random.randint(6, 10)
    data = {
        "generated_at": now.isoformat(),
        "pipeline_version": "1.0",
        "pipeline_runs": pipeline_runs,
        "stats": {
            "total_rejections": len(articles),
            "by_reason": by_reason,
            "by_category": by_category,
        },
        "hierarchy": hierarchy,
        "articles": articles,
    }

    os.makedirs("site/data", exist_ok=True)
    with open("site/data/near_misses.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    # Run history
    run_history = generate_run_history(now)
    with open("site/data/run_history.json", "w", encoding="utf-8") as f:
        json.dump(run_history, f, ensure_ascii=False, indent=2)

    # Recent additions
    recent_additions = generate_recent_additions(now, articles)
    with open("site/data/recent_additions.json", "w", encoding="utf-8") as f:
        json.dump(recent_additions, f, ensure_ascii=False, indent=1)

    # Rescue queue
    rescue_queue = generate_rescue_queue(articles)
    with open("site/data/rescue_queue.json", "w", encoding="utf-8") as f:
        json.dump(rescue_queue, f, ensure_ascii=False, indent=2)

    # Flagged papers (inverse bot)
    flagged_papers = generate_flagged_papers(now)
    with open("site/data/flagged_papers.json", "w", encoding="utf-8") as f:
        json.dump(flagged_papers, f, ensure_ascii=False, indent=1)

    recurring = sum(1 for a in articles if a.get("seen_count", 1) > 1)
    print(f"Generated {len(articles)} synthetic near-miss articles")
    print(f"  Genes: {len(GENES)} subcollections")
    print(f"  Topics: {sum(len(v) for v in TOPIC_HIERARCHY.values())} subcollections")
    print(f"  Shared (cross-subcollection): {shared_count}")
    print(f"  By reason: {by_reason}")
    print(f"  Recurring (seen_count > 1): {recurring}")
    print(f"  Pipeline runs: {pipeline_runs}")
    print(f"  Written to site/data/near_misses.json")
    print(f"Generated run history: {len(run_history['runs'])} runs")
    print(f"  Written to site/data/run_history.json")
    print(f"Generated recent additions: {len(recent_additions['additions'])} papers")
    print(f"  Written to site/data/recent_additions.json")
    print(f"Generated rescue queue: {len(rescue_queue)} entries")
    print(f"  Written to site/data/rescue_queue.json")
    print(f"Generated flagged papers: {len(flagged_papers['articles'])} orphans")
    print(f"  Written to site/data/flagged_papers.json")


if __name__ == "__main__":
    main()
