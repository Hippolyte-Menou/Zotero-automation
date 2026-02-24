#!/usr/bin/env python3
"""Generate realistic synthetic near_misses.json for dashboard testing.

Run from repo root:
    python generate_test_data.py

Output: site/data/near_misses.json
"""

import json
import random
import os
from datetime import datetime, timezone

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
        "Tunique externe", "Tunique interne", "Tunique intermediaire",
        "Cristallin", "Vitre", "Chambres et humeur aqueuse",
        "Voies optiques", "Annexes oculaires",
    ],
    "2 - Embryology": [
        "Vesicule optique", "Differenciation retinienne",
        "Crete neurale", "Cristallin embryonnaire",
    ],
    "3 - Physiology": [
        "Phototransduction", "Cycle visuel", "Neurotransmission retinienne",
        "Barriere hemato-retinienne", "Pression intraoculaire",
    ],
    "4 - Examinations": [
        "OCT", "ERG", "Champ visuel", "Angiographie",
        "Imagerie autofluorescence",
    ],
    "5 - Pathologies": [
        "Dystrophies retiniennes", "Glaucome", "Cataracte congenitale",
        "Neuropathies optiques", "Albinisme", "Dystrophies corneennes",
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


def random_authors(n=None):
    if n is None:
        n = random.randint(1, 6)
    return [
        [random.choice(FIRST_NAMES), random.choice(LAST_NAMES)]
        for _ in range(n)
    ]


def random_abstract():
    tmpl = random.choice(ABSTRACT_TEMPLATES)
    return tmpl.format(
        n=random.randint(2, 12),
        m=random.randint(10, 120),
        k=random.randint(500, 15000),
    )


def make_gene_article(gene, reason, pmid_counter):
    pmid = str(38000000 + pmid_counter)
    tmpl = random.choice(GENE_TITLE_TEMPLATES)
    title = tmpl.format(
        gene=gene,
        disease=random.choice(DISEASES),
        region=random.choice(REGIONS),
        model=random.choice(MODELS),
    )

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
    }

    if reason == "score_below_threshold":
        threshold = random.randint(3, 8)
        co_cit = random.randint(1, threshold - 1)
        bib = random.randint(0, 3)
        recency = random.randint(0, 3)
        eff = co_cit + min(bib, 3) + recency
        article.update({
            "co_citations": co_cit,
            "bib_coupling": bib,
            "recency_bonus": recency,
            "effective_score": eff,
            "threshold": threshold,
            "direction": random.choice(["citation", "reference", "both"]),
        })
    elif reason == "text_exclusion":
        article["matched_term"] = random.choice(TEXT_EXCLUSION_TERMS)
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
    }

    if reason == "score_below_threshold":
        threshold = random.randint(3, 6)
        co_cit = random.randint(1, threshold - 1)
        bib = random.randint(0, 3)
        recency = random.randint(0, 3)
        eff = co_cit + min(bib, 3) + recency
        article.update({
            "co_citations": co_cit,
            "bib_coupling": bib,
            "recency_bonus": recency,
            "effective_score": eff,
            "threshold": threshold,
            "direction": random.choice(["citation", "reference", "both"]),
        })
    elif reason == "text_exclusion":
        article["matched_term"] = random.choice(TEXT_EXCLUSION_TERMS)
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

    return article


def main():
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

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pipeline_version": "1.0",
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

    print(f"Generated {len(articles)} synthetic articles")
    print(f"  Genes: {len(GENES)} subcollections")
    print(f"  Topics: {sum(len(v) for v in TOPIC_HIERARCHY.values())} subcollections")
    print(f"  By reason: {by_reason}")
    print(f"  Written to site/data/near_misses.json")


if __name__ == "__main__":
    main()
