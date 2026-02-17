"""Map genes.yml tag slugs to PubMed search terms for disease-scoped queries."""

import logging

logger = logging.getLogger(__name__)

# Tag slug -> PubMed search term(s).
# MeSH terms use [MeSH], free-text terms use [tiab] (title/abstract).
# Some tags map to multiple terms (OR'd together) for better recall.
TAG_TO_PUBMED: dict[str, list[str]] = {
    # Retinal dystrophies
    "retinal-dystrophy": ['"Retinal Dystrophies"[MeSH]', '"retinal dystrophy"[tiab]'],
    "retinitis-pigmentosa": ['"Retinitis Pigmentosa"[MeSH]'],
    "cone-rod-dystrophy": ['"Cone-Rod Dystrophies"[MeSH]', '"cone-rod dystrophy"[tiab]'],
    "rod-cone-dystrophy": ['"Retinitis Pigmentosa"[MeSH]', '"rod-cone dystrophy"[tiab]'],
    "cone-dystrophy": ['"Color Vision Defects"[MeSH]', '"cone dystrophy"[tiab]'],
    "leber-congenital-amaurosis": ['"Leber Congenital Amaurosis"[MeSH]'],
    "stargardt-disease": ['"Stargardt Disease"[MeSH]', '"Stargardt"[tiab]'],
    "congenital-stationary-night-blindness": ['"Night Blindness"[MeSH]', '"congenital stationary night blindness"[tiab]'],
    "achromatopsia": ['"Color Vision Defects"[MeSH]', '"achromatopsia"[tiab]'],
    "choroideremia": ['"Choroideremia"[MeSH]'],
    "retinoschisis": ['"Retinoschisis"[MeSH]'],
    "enhanced-s-cone-syndrome": ['"enhanced S-cone syndrome"[tiab]'],
    "retinal-degeneration": ['"Retinal Degeneration"[MeSH]'],
    "retinal-detachment": ['"Retinal Detachment"[MeSH]'],
    "bestrophinopathy": ['"Vitelliform Macular Dystrophy"[MeSH]', '"bestrophinopathy"[tiab]'],
    "vitelliform": ['"Vitelliform Macular Dystrophy"[MeSH]'],
    "blue-cone-monochromacy": ['"blue cone monochromacy"[tiab]'],
    "color-vision-deficiency": ['"Color Vision Defects"[MeSH]'],
    "gyrate-atrophy": ['"Gyrate Atrophy"[MeSH]'],
    "chorioretinal-atrophy": ['"chorioretinal atrophy"[tiab]', '"Choroid Diseases"[MeSH]'],
    "choroidal-dystrophy": ['"choroidal dystrophy"[tiab]', '"Choroid Diseases"[MeSH]'],
    "cone-rod-synaptic-disorder": ['"cone rod synaptic disorder"[tiab]', '"Retinal Dystrophies"[MeSH]'],
    "aaland-island-eye-disease": ['"Aland Island eye disease"[tiab]', '"ocular albinism"[tiab]'],
    # Macular dystrophies
    "macular-dystrophy": ['"Macular Degeneration"[MeSH]', '"macular dystrophy"[tiab]'],
    "macular-degeneration": ['"Macular Degeneration"[MeSH]'],
    "age-related-macular-degeneration": ['"Macular Degeneration"[MeSH]', '"age-related macular degeneration"[tiab]'],
    "macular": ['"Macular Degeneration"[MeSH]', '"macular"[tiab]'],
    "patterned": ['"pattern dystrophy"[tiab]', '"Retinal Dystrophies"[MeSH]'],
    # Ciliopathies / syndromic
    "ciliopathy": ['"Ciliopathies"[MeSH]', '"ciliopathy"[tiab]'],
    "usher-syndrome": ['"Usher Syndromes"[MeSH]'],
    "bardet-biedl": ['"Bardet-Biedl Syndrome"[MeSH]'],
    "joubert-syndrome": ['"Joubert Syndrome"[MeSH]', '"Joubert"[tiab]'],
    "meckel-syndrome": ['"Encephalocele"[MeSH]', '"Meckel"[tiab]'],
    "senior-loken": ['"senior-loken"[tiab]', '"nephronophthisis"[tiab]'],
    "alstrom-syndrome": ['"Alstrom Syndrome"[MeSH]', '"Alstrom"[tiab]'],
    # Anterior segment
    "anterior-segment-dysgenesis": ['"anterior segment dysgenesis"[tiab]', '"Eye Abnormalities"[MeSH]'],
    "axenfeld-rieger": ['"Axenfeld"[tiab]', '"Rieger"[tiab]'],
    "aniridia": ['"Aniridia"[MeSH]'],
    "coloboma": ['"Coloboma"[MeSH]'],
    "microphthalmia": ['"Microphthalmos"[MeSH]'],
    "structural-eye-disease": ['"Eye Abnormalities"[MeSH]'],
    # Corneal
    "corneal-dystrophy": ['"Corneal Dystrophies, Hereditary"[MeSH]', '"corneal dystrophy"[tiab]'],
    "corneal-disease": ['"Corneal Diseases"[MeSH]'],
    "fuchs": ['"Fuchs Endothelial Dystrophy"[MeSH]', '"Fuchs"[tiab]'],
    "keratitis": ['"Keratitis"[MeSH]'],
    "stromal": ['"corneal stromal dystrophy"[tiab]'],
    "endothelial": ['"corneal endothelial dystrophy"[tiab]'],
    "epithelial": ['"corneal epithelial dystrophy"[tiab]'],
    "lattice": ['"lattice dystrophy"[tiab]'],
    "dermoid": ['"Dermoid Cyst"[MeSH]', '"dermoid"[tiab]'],
    "megalocornea": ['"Megalocornea"[MeSH]', '"megalocornea"[tiab]'],
    # Lens
    "congenital-cataract": ['"Cataract"[MeSH]', '"congenital cataract"[tiab]'],
    "ectopia-lentis": ['"Ectopia Lentis"[MeSH]'],
    "marfan-syndrome": ['"Marfan Syndrome"[MeSH]'],
    # Glaucoma
    "glaucoma": ['"Glaucoma"[MeSH]'],
    # Vitreoretinal
    "vitreoretinopathy": ['"Vitreoretinopathy, Proliferative"[MeSH]', '"vitreoretinopathy"[tiab]'],
    "stickler-syndrome": ['"connective tissue disease"[MeSH]', '"Stickler"[tiab]'],
    "norrie-disease": ['"Norrie Disease"[MeSH]', '"Norrie"[tiab]'],
    # Optic nerve
    "optic-atrophy": ['"Optic Atrophy"[MeSH]'],
    "optic-neuropathy": ['"Optic Nerve Diseases"[MeSH]'],
    "optic-nerve-hypoplasia": ['"Optic Nerve Diseases"[MeSH]', '"optic nerve hypoplasia"[tiab]'],
    # Other
    "albinism": ['"Albinism"[MeSH]'],
    "myopia": ['"Myopia"[MeSH]'],
    "nystagmus": ['"Nystagmus, Pathologic"[MeSH]', '"nystagmus"[tiab]'],
    "foveal-hypoplasia": ['"foveal hypoplasia"[tiab]'],
    "syndromic": ['"Syndrome"[MeSH]'],
}

# Fallback for genes with no tags or no mapped tags
DEFAULT_EYE_TERMS = (
    '"Eye Diseases"[MeSH] OR "Retina"[MeSH] OR "Optic Nerve"[MeSH] '
    'OR "Cornea"[MeSH] OR "Lens, Crystalline"[MeSH] OR "eye"[tiab] '
    'OR "retina"[tiab] OR "retinal"[tiab] OR "ocular"[tiab] '
    'OR "ophthalmic"[tiab] OR "optic"[tiab]'
)


def get_disease_query_terms(tags: list[str]) -> str:
    """Build a PubMed AND clause from gene tag slugs.

    Returns a parenthesized OR group of all disease terms, or the default
    eye terms fallback if no tags map to known terms.
    """
    terms: list[str] = []
    unmapped: list[str] = []

    for tag in tags:
        if tag in TAG_TO_PUBMED:
            terms.extend(TAG_TO_PUBMED[tag])
        else:
            unmapped.append(tag)

    if unmapped:
        logger.debug(f"Unmapped tags (no PubMed terms): {unmapped}")

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_terms: list[str] = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            unique_terms.append(t)

    if unique_terms:
        return "(" + " OR ".join(unique_terms) + ")"

    logger.info("No disease tags mapped, using default eye terms")
    return "(" + DEFAULT_EYE_TERMS + ")"
