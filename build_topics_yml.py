"""Build topics.yml from vault content and taxonomy data.

Regenerates Category 5 (Pathologies) sub_topics from disease_name_mapping.json.
Enriches all categories with MeSH descriptor names from taxonomy.yaml terminology.
Preserves all manual configuration and per-sub-topic overrides.

Usage:
    python build_topics_yml.py                    # Build and write topics.yml
    python build_topics_yml.py --dry-run          # Print to stdout only
    python build_topics_yml.py --vault-path PATH  # Custom vault root
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# FR MOC category -> EN sub_topic name
MOC_CATEGORY_TO_SUBTOPIC: dict[str, str] = {
    "Albinismes": "Albinism",
    "Anomalies du développement oculaire": "Ocular developmental anomalies",
    "Cataractes congénitales": "Congenital cataracts",
    "Dystrophies cornéennes": "Corneal dystrophies",
    "Dystrophies rétiniennes": "Retinal dystrophies",
}

# pathology_tag prefixes that route to Vitreoretinopathies instead of
# Retinal dystrophies (used for the split within "Dystrophies retiniennes")
VITREORETINOPATH_TAG_PREFIXES = ("pathology/vitreoretinopathy",)

# taxonomy.yaml terminology category -> topics.yml sub_topic names
MESH_CATEGORY_TO_SUBTOPICS: dict[str, list[str]] = {
    # Categories 1-4
    "anatomy-retina": ["Inner coat", "Specialized microanatomy"],
    "anatomy-cornea": ["Outer coat"],
    "anatomy-other": [
        "Middle coat",
        "Transparent media",
        "Vascularization and innervation",
        "Orbit",
    ],
    "physiology": ["Visual physiology"],
    "clinical": [
        "Ocular imaging",
        "Specialized functional testing",
        "Visual function",
    ],
    # Category 5 (pathologies) -- omitted from category-level mapping because
    # pathology MeSH terms need per-term routing (see MESH_TERM_TO_SUBTOPIC).
}

# Fine-grained per-term MeSH routing for pathology terms.
# Maps individual MeSH EN descriptor names to their correct sub_topic(s).
MESH_TERM_TO_SUBTOPIC: dict[str, list[str]] = {
    # pathology-retinal
    "Retinitis pigmentosa": ["Retinal dystrophies"],
    "Cone-rod dystrophy": ["Retinal dystrophies"],
    "Stargardt disease": ["Retinal dystrophies"],
    "Vitelliform macular dystrophy": ["Retinal dystrophies"],
    "Leber congenital amaurosis": ["Retinal dystrophies"],
    "Choroideremia": ["Retinal dystrophies"],
    "Retinoschisis": ["Retinal dystrophies"],
    # pathology-corneal
    "Corneal dystrophy": ["Corneal dystrophies"],
    "Fuchs endothelial dystrophy": ["Corneal dystrophies"],
    "Lattice corneal dystrophy": ["Corneal dystrophies"],
    "Granular corneal dystrophy": ["Corneal dystrophies"],
    # pathology-other: each term goes to the correct sub_topic
    "Congenital cataract": ["Congenital cataracts"],
    "Ocular albinism": ["Albinism"],
    "Oculocutaneous albinism": ["Albinism"],
    "Aniridia": ["Ocular developmental anomalies"],
    "Congenital glaucoma": ["Ocular developmental anomalies"],
    "Age-related macular degeneration": ["Retinal dystrophies"],
    "High myopia": ["Retinal dystrophies"],
    "Hereditary optic neuropathy": ["Retinal dystrophies"],
}

# Vault folder -> topics.yml category name (for reconciliation)
VAULT_FOLDER_MAP: dict[str, str] = {
    "B - Anatomie": "1 - Anatomy",
    "C - Embryologie": "2 - Embryology",
    "D - Physiologie": "3 - Physiology",
    "E - Examens": "4 - Examinations",
}

# Known disease name synonyms: maps alternate names to canonical en_name.
# When multiple taxonomy/override entries refer to the same disease under
# different names, the canonical form is kept and the alternate is merged.
DISEASE_SYNONYMS: dict[str, str] = {
    "Anophthalmia/microphthalmia": "Anophthalmia and microphthalmia",
    "Blue cone monochromatism": "Blue cone monochromacy",
    "Vitelliform macular dystrophy": "Best vitelliform macular dystrophy",
    "Gyrate atrophy": "Gyrate atrophy of choroid and retina",
    "Exudative vitreoretinopathy": "Familial exudative vitreoretinopathy",
    "Wagner syndrome": "Wagner vitreoretinopathy",
}

# Inverted disease names that need reformulation for search.
# "Name, qualifier" -> "qualifier name"
_INVERTED_NAME_RE = re.compile(r"^(.+?),\s+(.+)$")


# ---------------------------------------------------------------------------
# Custom YAML representer -- flow-style for short lists
# ---------------------------------------------------------------------------

class _FlowList(list):
    """Marker subclass to signal flow-style serialization."""


def _flow_list_representer(dumper: yaml.Dumper, data: _FlowList) -> yaml.Node:
    return dumper.represent_sequence(
        "tag:yaml.org,2002:seq", data, flow_style=True
    )


yaml.add_representer(_FlowList, _flow_list_representer)


# ---------------------------------------------------------------------------
# Vault discovery
# ---------------------------------------------------------------------------

def find_vault_root(script_dir: Path) -> Path:
    """Walk up from script location to find the vault root.

    Expects: vault/_assets/code/Zotero-automation/build_topics_yml.py
    """
    candidate = script_dir.parent.parent.parent  # up 3 levels
    if (candidate / "B - Anatomie").is_dir():
        return candidate
    raise FileNotFoundError(
        f"Cannot find vault root (expected 'B - Anatomie' at {candidate}). "
        "Use --vault-path to specify manually."
    )


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_taxonomy(vault_root: Path) -> dict:
    """Load taxonomy.yaml from the gene-generation data directory."""
    path = (
        vault_root
        / "_assets"
        / "code"
        / "gene-generation"
        / "data"
        / "taxonomy.yaml"
    )
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_disease_mapping(vault_root: Path) -> dict:
    """Load disease_name_mapping.json (pre-built by build_disease_name_mapping.py)."""
    path = (
        vault_root
        / "_assets"
        / "code"
        / "gene-generation"
        / "data"
        / "disease_name_mapping.json"
    )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_existing_config(topics_yml_path: Path) -> dict:
    """Load existing topics.yml to preserve config and overrides."""
    if not topics_yml_path.exists():
        return {}
    text = topics_yml_path.read_text(encoding="utf-8")
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return {}


# ---------------------------------------------------------------------------
# Override preservation
# ---------------------------------------------------------------------------

def build_subtopic_overrides(
    existing_config: dict,
) -> dict[str, dict[str, dict]]:
    """Build (category_name, sub_topic_name) -> preserved fields index.

    For categories 1-4: preserves keywords, clinical_scope, max_results,
    type_filter, collection.
    For category 5: preserves max_results, collection.
    """
    index: dict[str, dict[str, dict]] = {}
    for cat in existing_config.get("categories", []):
        cat_name = cat.get("name", "")
        cat_overrides: dict[str, dict] = {}
        for sub in cat.get("sub_topics", []):
            sub_name = sub.get("name", "")
            preserved: dict = {}
            # Always preserve these
            for key in ("max_results", "type_filter", "collection"):
                if key in sub:
                    preserved[key] = sub[key]
            # Categories 1-4: preserve keywords and clinical_scope
            if not cat_name.startswith("5"):
                for key in ("keywords", "clinical_scope"):
                    if key in sub:
                        preserved[key] = sub[key]
            if preserved:
                cat_overrides[sub_name] = preserved
        if cat_overrides:
            index[cat_name] = cat_overrides
    return index


def build_disease_overrides(
    existing_config: dict,
) -> dict[str, dict[str, list[str]]]:
    """Build (sub_topic_name, disease_name) -> en_keywords index.

    Preserves hand-curated en_keywords from existing Category 5 diseases.
    """
    index: dict[str, dict[str, list[str]]] = {}
    for cat in existing_config.get("categories", []):
        if not cat.get("name", "").startswith("5"):
            continue
        for sub in cat.get("sub_topics", []):
            sub_name = sub.get("name", "")
            disease_kw: dict[str, list[str]] = {}
            for disease in sub.get("diseases", []):
                d_name = disease.get("name", "")
                if "en_keywords" in disease:
                    disease_kw[d_name] = disease["en_keywords"]
            if disease_kw:
                index[sub_name] = disease_kw
        break  # only one category 5
    return index


def build_category_overrides(
    existing_config: dict,
) -> dict[str, dict]:
    """Build category_name -> category-level preserved fields.

    Preserves: topic_ids, type_filter, citation_expansion, clinical_keywords,
    collection.
    """
    index: dict[str, dict] = {}
    for cat in existing_config.get("categories", []):
        cat_name = cat.get("name", "")
        preserved: dict = {}
        for key in (
            "topic_ids",
            "type_filter",
            "citation_expansion",
            "clinical_keywords",
            "collection",
        ):
            if key in cat:
                preserved[key] = cat[key]
        if preserved:
            index[cat_name] = preserved
    return index


# ---------------------------------------------------------------------------
# Category 5: pathology generation
# ---------------------------------------------------------------------------

def _reformulate_en_name(en_name: str) -> list[str]:
    """Convert an EN disease name into search keywords.

    Handles:
    - Compound names with "/": split into separate keywords
    - Inverted names ("X, Y"): reformulate to "Y X"
    - Normal names: return as-is
    """
    # Split compound names on "/"
    if "/" in en_name:
        parts = [p.strip() for p in en_name.split("/") if p.strip()]
        keywords = []
        for part in parts:
            keywords.extend(_reformulate_en_name(part))
        return keywords

    # Reformulate inverted names: "Night blindness, congenital stationary"
    # -> "congenital stationary night blindness"
    # "Corneal dystrophy, Fleck" -> "Fleck corneal dystrophy"
    m = _INVERTED_NAME_RE.match(en_name)
    if m:
        base = m.group(1)
        qualifier = m.group(2)
        # Lowercase the base when moving it after the qualifier,
        # unless it starts with a proper noun (keep first word of qualifier as-is)
        reformulated = f"{qualifier} {base[0].lower() + base[1:]}"
        return [reformulated.strip()]

    return [en_name]


def _normalize_disease_name(name: str) -> str:
    """Normalize a disease name for dedup comparison.

    Strips case, punctuation, filler words (and/of/the), and sorts
    remaining words to match 'Corneal dystrophy, Fleck' ==
    'Fleck corneal dystrophy', etc.
    """
    s = name.lower()
    s = re.sub(r"[,/\-]", " ", s)
    words = [w for w in s.split() if w not in ("and", "of", "the")]
    return " ".join(sorted(words))


def build_pathology_subtopics(
    disease_mapping: dict,
    disease_overrides: dict[str, dict[str, list[str]]],
    subtopic_overrides: dict[str, dict],
) -> list[dict]:
    """Build Category 5 sub_topics from disease_name_mapping.json.

    Groups diseases by moc_category, deduplicates by en_name, splits
    vitreoretinopathies from retinal dystrophies. Preserves hand-curated
    diseases from existing topics.yml that aren't in the taxonomy.

    Override resolution: for each taxonomy disease, the script checks
    the existing topics.yml for a disease entry with matching name
    (normalized comparison). If found, the hand-curated en_keywords
    are used instead of auto-generated ones.

    Extra diseases in existing topics.yml that have no taxonomy match
    (e.g. Stickler syndrome) are appended to preserve them.
    """
    # Step 1: Group taxonomy entries by target sub_topic
    groups: dict[str, dict[str, dict]] = defaultdict(dict)

    for _fr_name, entry in disease_mapping.items():
        if entry.get("is_category_header"):
            continue
        en_name = entry.get("en_name")
        if not en_name:
            continue

        # Canonicalize known synonyms
        en_name = DISEASE_SYNONYMS.get(en_name, en_name)

        moc_cat = entry.get("moc_category", "")
        pathology_tag = entry.get("pathology_tag", "")

        is_vitreo = any(
            pathology_tag.startswith(prefix)
            for prefix in VITREORETINOPATH_TAG_PREFIXES
        )
        if is_vitreo:
            target = "Vitreoretinopathies"
        else:
            target = MOC_CATEGORY_TO_SUBTOPIC.get(moc_cat)
            if not target:
                print(
                    f"  [WARN] Unknown moc_category '{moc_cat}' for "
                    f"'{en_name}', skipping",
                    file=sys.stderr,
                )
                continue

        if en_name not in groups[target]:
            groups[target][en_name] = entry

    # Step 2: Build sub_topic dicts
    ordered_subtopics = list(MOC_CATEGORY_TO_SUBTOPIC.values())
    if "Vitreoretinopathies" not in ordered_subtopics:
        ordered_subtopics.append("Vitreoretinopathies")

    cat5_overrides = subtopic_overrides.get("5 - Pathologies", {})
    result: list[dict] = []

    for sub_name in ordered_subtopics:
        sub_disease_overrides = disease_overrides.get(sub_name, {})

        # Build normalized index of existing override names for dedup.
        # Apply synonym canonicalization to override names too.
        norm_override_index: dict[str, tuple[str, list[str]]] = {}
        for ovr_name, ovr_kw in sub_disease_overrides.items():
            canonical = DISEASE_SYNONYMS.get(ovr_name, ovr_name)
            norm_override_index[_normalize_disease_name(canonical)] = (
                canonical,
                ovr_kw,
            )

        # Track which overrides get matched (to find unmatched extras)
        matched_override_norms: set[str] = set()
        # Track normalized taxonomy names (to dedup extra overrides)
        taxonomy_norms: set[str] = set()

        diseases_list: list[dict] = []

        if sub_name in groups:
            for en_name in sorted(groups[sub_name].keys()):
                norm = _normalize_disease_name(en_name)
                taxonomy_norms.add(norm)

                # Also check normalized form of reformulated name
                reformulated = _reformulate_en_name(en_name)
                reform_norms = {_normalize_disease_name(r) for r in reformulated}

                # Try to find matching override
                override_kw = None
                for check_norm in [norm] + list(reform_norms):
                    if check_norm in norm_override_index:
                        _, override_kw = norm_override_index[check_norm]
                        matched_override_norms.add(check_norm)
                        break

                if override_kw is not None:
                    en_keywords = override_kw
                else:
                    en_keywords = reformulated

                diseases_list.append({
                    "name": en_name,
                    "en_keywords": _FlowList(en_keywords),
                })

        # Step 3: Merge unmatched hand-curated diseases from existing config
        for ovr_norm, (ovr_name, ovr_kw) in norm_override_index.items():
            if ovr_norm in matched_override_norms:
                continue
            if ovr_norm in taxonomy_norms:
                continue
            diseases_list.append({
                "name": ovr_name,
                "en_keywords": _FlowList(ovr_kw),
            })

        diseases_list.sort(key=lambda d: d["name"])

        if not diseases_list:
            continue

        sub_topic: dict = {
            "name": sub_name,
            "collection": sub_name,
        }

        if sub_name in cat5_overrides:
            for key in ("collection", "max_results"):
                if key in cat5_overrides[sub_name]:
                    sub_topic[key] = cat5_overrides[sub_name][key]

        sub_topic["diseases"] = diseases_list

        if "max_results" not in sub_topic:
            sub_topic["max_results"] = 30

        result.append(sub_topic)

    return result


# ---------------------------------------------------------------------------
# MeSH enrichment
# ---------------------------------------------------------------------------

def build_mesh_enrichment(taxonomy: dict) -> dict[str, list[str]]:
    """Build sub_topic_name -> list of MeSH EN descriptor names.

    Reads terminology section from taxonomy.yaml. Uses two routing strategies:
    - Category-level: MESH_CATEGORY_TO_SUBTOPICS (for anatomy, physiology, clinical)
    - Per-term: MESH_TERM_TO_SUBTOPIC (for pathology terms that need fine-grained routing)
    """
    mesh_map: dict[str, list[str]] = defaultdict(list)

    for term in taxonomy.get("terminology", []):
        category = term.get("category", "")
        en_name = term.get("en", "")
        if not en_name:
            continue

        # Check per-term routing first (pathology terms)
        if en_name in MESH_TERM_TO_SUBTOPIC:
            for sub_name in MESH_TERM_TO_SUBTOPIC[en_name]:
                if en_name not in mesh_map[sub_name]:
                    mesh_map[sub_name].append(en_name)
        # Fall back to category-level routing
        elif category in MESH_CATEGORY_TO_SUBTOPICS:
            for sub_name in MESH_CATEGORY_TO_SUBTOPICS[category]:
                if en_name not in mesh_map[sub_name]:
                    mesh_map[sub_name].append(en_name)

    return dict(mesh_map)


def apply_mesh_enrichment(
    categories: list[dict],
    mesh_map: dict[str, list[str]],
) -> int:
    """Add mesh_terms field to matching sub_topics across all categories.

    Returns the number of sub_topics enriched.
    """
    count = 0
    for cat in categories:
        for sub in cat.get("sub_topics", []):
            sub_name = sub.get("name", "")
            if sub_name in mesh_map:
                sub["mesh_terms"] = _FlowList(mesh_map[sub_name])
                count += 1
    return count


# ---------------------------------------------------------------------------
# Vault folder reconciliation
# ---------------------------------------------------------------------------

def reconcile_vault_folders(
    vault_root: Path,
    categories: list[dict],
) -> list[str]:
    """Compare vault subfolders against topics.yml sub_topics.

    Returns list of diagnostic messages.
    """
    messages: list[str] = []

    for vault_folder, cat_name in VAULT_FOLDER_MAP.items():
        folder_path = vault_root / vault_folder
        if not folder_path.is_dir():
            messages.append(f"  [WARN] Vault folder '{vault_folder}' not found")
            continue

        # Get vault subfolder names (strip number prefix)
        vault_subs: set[str] = set()
        for d in sorted(folder_path.iterdir()):
            if d.is_dir():
                name = d.name
                # Strip "NN - " prefix
                stripped = re.sub(r"^\d+\s*-\s*", "", name).strip()
                if stripped:
                    vault_subs.add(stripped)

        # Get topics.yml sub_topic names for this category
        topic_subs: set[str] = set()
        for cat in categories:
            if cat.get("name") == cat_name:
                for sub in cat.get("sub_topics", []):
                    topic_subs.add(sub.get("name", ""))
                break

        # Report differences
        new_in_vault = vault_subs - topic_subs
        # Note: we don't report sub_topics missing from vault since some
        # sub_topics are conceptual groupings without a dedicated folder

        if new_in_vault:
            messages.append(
                f"  [NEW] {cat_name}: vault subfolders not in topics.yml: "
                f"{', '.join(sorted(new_in_vault))}"
            )
        else:
            messages.append(f"  [OK] {cat_name}: all vault subfolders covered")

    return messages


# ---------------------------------------------------------------------------
# YAML assembly
# ---------------------------------------------------------------------------

def build_topics_yml(vault_root: Path, topics_yml_path: Path) -> str:
    """Build the full topics.yml content."""
    existing = load_existing_config(topics_yml_path)
    subtopic_ovr = build_subtopic_overrides(existing)
    disease_ovr = build_disease_overrides(existing)
    category_ovr = build_category_overrides(existing)

    taxonomy = load_taxonomy(vault_root)
    disease_mapping = load_disease_mapping(vault_root)

    # ---- Preserve top-level config ----
    search = existing.get("search", {
        "max_results": 50,
        "recent_max_results": 5,
        "type_filter": "review|article",
    })
    citation_expansion = existing.get("citation_expansion", {
        "enabled": True,
        "max_seed_papers": 30,
        "min_co_citations": 2,
        "max_min_co": 4,
        "max_hops": 1,
        "hop2_top_n": 5,
    })
    openalex_scoping = existing.get("openalex_scoping", {
        "ophthalmology_subfield": 2731,
    })
    default_excl_text = existing.get("default_exclusions_text", [])
    default_excl_mesh = existing.get("default_exclusions_mesh", [])

    # ---- Build categories ----
    # Preserve categories 1-4 verbatim from existing config
    existing_categories = existing.get("categories", [])
    cats_1_4 = [
        cat for cat in existing_categories
        if not cat.get("name", "").startswith("5")
    ]

    # Build Category 5
    cat5_existing = next(
        (cat for cat in existing_categories if cat.get("name", "").startswith("5")),
        {},
    )
    pathology_subtopics = build_pathology_subtopics(
        disease_mapping, disease_ovr, subtopic_ovr
    )

    cat5: dict = {"name": "5 - Pathologies"}
    # Apply category-level overrides
    cat5_ovr = category_ovr.get("5 - Pathologies", {})
    for key in ("collection", "topic_ids", "type_filter", "clinical_keywords",
                "citation_expansion"):
        if key in cat5_ovr:
            cat5[key] = cat5_ovr[key]
        elif key in cat5_existing:
            cat5[key] = cat5_existing[key]
    cat5["sub_topics"] = pathology_subtopics

    categories = cats_1_4 + [cat5]

    # ---- MeSH enrichment ----
    mesh_map = build_mesh_enrichment(taxonomy)
    mesh_count = apply_mesh_enrichment(categories, mesh_map)
    mesh_terms_total = sum(len(v) for v in mesh_map.values())

    # ---- Vault reconciliation ----
    recon_messages = reconcile_vault_folders(vault_root, categories)

    # ---- Count stats ----
    disease_count = sum(
        len(sub.get("diseases", []))
        for sub in pathology_subtopics
    )

    print(
        f"Category 5 (Pathologies): {len(pathology_subtopics)} sub_topics, "
        f"{disease_count} diseases",
        file=sys.stderr,
    )
    print(
        f"MeSH enrichment: {mesh_count} sub_topics enriched "
        f"with {mesh_terms_total} terms total",
        file=sys.stderr,
    )
    print("Vault reconciliation:", file=sys.stderr)
    for msg in recon_messages:
        print(msg, file=sys.stderr)

    # ---- Assemble ----
    config = {
        "search": search,
        "citation_expansion": citation_expansion,
        "openalex_scoping": openalex_scoping,
        "default_exclusions_text": default_excl_text,
        "default_exclusions_mesh": default_excl_mesh,
        "categories": categories,
    }

    header = (
        "# topics.yml\n"
        "# Auto-generated by build_topics_yml.py from vault taxonomy data.\n"
        "#\n"
        "# What is regenerated on each run:\n"
        "#   - Category 5 (Pathologies) disease lists from disease_name_mapping.json\n"
        "#   - mesh_terms fields from taxonomy.yaml terminology section\n"
        "#\n"
        "# What is preserved across rebuilds:\n"
        "#   - All top-level config (search, citation_expansion, openalex_scoping, exclusions)\n"
        "#   - Categories 1-4 keywords, clinical_scope, max_results, type_filter\n"
        "#   - Category 5 clinical_keywords, citation_expansion, per-sub-topic max_results\n"
        "#   - Hand-curated per-disease en_keywords overrides\n"
        "#\n"
        "# To add a disease not in the taxonomy: add it to taxonomy.yaml disease_name_mapping,\n"
        "# run build_disease_name_mapping.py, then re-run this script.\n"
        "#\n"
        "# To add MeSH terms: add them to taxonomy.yaml terminology section,\n"
        "# then re-run this script.\n"
        "\n"
    )

    body = yaml.dump(
        config,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        width=120,
    )

    return header + body


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build topics.yml from vault content and taxonomy data."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print output to stdout without writing the file.",
    )
    parser.add_argument(
        "--vault-path",
        type=Path,
        default=None,
        help="Path to the vault root (default: auto-detect from script location).",
    )
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    vault_root = args.vault_path or find_vault_root(script_dir)
    vault_root = Path(vault_root).resolve()

    topics_yml_path = script_dir / "topics.yml"

    print(f"Vault root: {vault_root}", file=sys.stderr)
    print(f"Output: {topics_yml_path}", file=sys.stderr)

    output = build_topics_yml(vault_root, topics_yml_path)

    if args.dry_run:
        print(output)
        print("[DRY RUN] Would write to topics.yml", file=sys.stderr)
    else:
        topics_yml_path.write_text(output, encoding="utf-8")
        print(f"Written to {topics_yml_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
