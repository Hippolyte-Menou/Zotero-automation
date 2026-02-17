"""Build genes.yml from vault gene notes.

Scans G - Genes/*.md for YAML frontmatter, extracts pathology tags,
and generates a genes.yml compatible with the Zotero-automation pipeline.

Preserves existing collections, citation expansion config, and per-gene
exclude_text overrides from the current genes.yml.

Usage:
    python build_genes_yml.py                    # Build and write genes.yml
    python build_genes_yml.py --dry-run          # Print to stdout only
    python build_genes_yml.py --vault-path PATH  # Custom vault root
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml


def find_vault_root(script_dir: Path) -> Path:
    """Walk up from script location to find the vault root.

    Expects: vault/_assets/code/Zotero-automation/build_genes_yml.py
    """
    # script_dir is Zotero-automation/
    candidate = script_dir.parent.parent.parent  # up 3 levels
    genes_dir = candidate / "G - Genes"
    if genes_dir.is_dir():
        return candidate
    raise FileNotFoundError(
        f"Cannot find vault root (expected 'G - Genes' at {genes_dir}). "
        "Use --vault-path to specify manually."
    )


def parse_frontmatter(filepath: Path) -> dict | None:
    """Extract YAML frontmatter from an Obsidian markdown file."""
    text = filepath.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return None
    try:
        return yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None


def extract_pathology_tags(tags: list[str]) -> list[str]:
    """Extract deduplicated disease tags from pathology/ tag paths.

    From 'pathology/retinal-dystrophy/retinitis-pigmentosa':
      - leaf: 'retinitis-pigmentosa'
      - intermediate segments: 'retinal-dystrophy'
      (excludes 'pathology' itself)
    """
    result: set[str] = set()
    for tag in tags:
        if not tag.startswith("pathology/"):
            continue
        parts = tag.split("/")
        # parts[0] is 'pathology', skip it; keep all others
        for segment in parts[1:]:
            result.add(segment)
    return sorted(result)


def load_existing_config(genes_yml_path: Path) -> dict:
    """Load existing genes.yml to preserve defaults and per-gene overrides."""
    if not genes_yml_path.exists():
        return {}
    text = genes_yml_path.read_text(encoding="utf-8")
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return {}


def build_override_index(existing_config: dict) -> dict[str, dict]:
    """Build a symbol -> overrides mapping from existing genes.yml."""
    overrides: dict[str, dict] = {}
    for gene in existing_config.get("genes", []):
        symbol = gene.get("symbol", "")
        entry: dict = {}
        if "exclude_text" in gene:
            entry["exclude_text"] = gene["exclude_text"]
        if entry:
            overrides[symbol] = entry
    return overrides


def scan_gene_notes(vault_root: Path) -> list[dict]:
    """Scan G - Genes/*.md and extract gene symbol + pathology tags."""
    genes_dir = vault_root / "G - Genes"
    results = []
    skipped = []

    for md_file in sorted(genes_dir.glob("*.md")):
        fm = parse_frontmatter(md_file)
        if fm is None:
            continue

        symbol = fm.get("Symbol")
        if not symbol:
            continue

        tags = fm.get("tags", [])
        if not isinstance(tags, list):
            continue

        pathology_tags = extract_pathology_tags(tags)
        if not pathology_tags:
            skipped.append(symbol)
            continue

        results.append({
            "symbol": symbol,
            "tags": pathology_tags,
        })

    if skipped:
        print(
            f"Skipped {len(skipped)} genes without pathology tags: "
            f"{', '.join(skipped)}",
            file=sys.stderr,
        )

    return results


def build_genes_yml(vault_root: Path, genes_yml_path: Path) -> str:
    """Build the full genes.yml content."""
    existing = load_existing_config(genes_yml_path)
    overrides = build_override_index(existing)

    # Preserve top-level config or use defaults
    collections = existing.get("collections", {"genes_parent": "6 - Genes"})
    search = existing.get("search", {"max_results": 25, "recent_max_results": 10})
    citation_expansion = existing.get("citation_expansion", {
        "max_seed_papers": 100,
        "min_co_citations": 1,
        "max_min_co": 6,
        "max_hops": 2,
        "hop2_top_n": 10,
    })
    default_excl_text = existing.get("default_exclusions_text", [])

    gene_entries = scan_gene_notes(vault_root)

    # Merge per-gene overrides
    for entry in gene_entries:
        symbol = entry["symbol"]
        if symbol in overrides:
            entry.update(overrides[symbol])

    # Build final structure
    config = {
        "collections": collections,
        "search": search,
        "citation_expansion": citation_expansion,
        "default_exclusions_text": default_excl_text,
        "genes": [],
    }

    for entry in gene_entries:
        gene_block: dict = {
            "symbol": entry["symbol"],
            "collection": entry["symbol"],
            "tags": entry["tags"],
        }
        if "exclude_text" in entry:
            gene_block["exclude_text"] = entry["exclude_text"]
        config["genes"].append(gene_block)

    # Header comment
    header = (
        "# genes.yml\n"
        "# Auto-generated by build_genes_yml.py from vault gene notes.\n"
        "# Per-gene exclude_text overrides are preserved across rebuilds.\n"
        "# To add custom exclusions, edit the gene entry below and re-run.\n"
        "#\n"
        "# Tag strategy:\n"
        "#   - Gene symbol is always added automatically (no need to list it)\n"
        "#   - Work type tag is auto-derived from OpenAlex type field\n"
        "#   - Disease group tags below come from vault pathology/ tags\n"
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build genes.yml from vault gene notes."
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

    genes_yml_path = script_dir / "genes.yml"

    print(f"Vault root: {vault_root}", file=sys.stderr)
    print(f"Output: {genes_yml_path}", file=sys.stderr)

    output = build_genes_yml(vault_root, genes_yml_path)

    gene_count = output.count("  symbol:")
    print(f"Generated {gene_count} gene entries.", file=sys.stderr)

    if args.dry_run:
        print(output)
    else:
        genes_yml_path.write_text(output, encoding="utf-8")
        print(f"Written to {genes_yml_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
