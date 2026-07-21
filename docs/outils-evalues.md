# Outils évalués — gardés, écartés, et POURQUOI

> **But : ne pas refaire deux fois la même erreur.** Chaque outil essayé laisse une trace ici, avec
> le motif **mesuré** de la décision (jamais « ça avait l'air lent » : un chiffre, une date, une
> ancre). Un outil écarté peut être ré-évalué — mais alors on part de ce qui est écrit, pas de zéro.
>
> **Règle d'écriture** : une ligne n'entre ici qu'après un essai RÉEL. Pas d'avis de catalogue.
> Si la raison du rejet cesse d'être vraie (nouvelle version, autre hardware), le noter plutôt que
> réécrire l'histoire.

## Lecture / OCR

| Outil | Verdict | Pourquoi (mesuré) |
|---|---|---|
| **PyMuPDF** (couche texte) | ✅ **Gardé** — lecteur primaire born-digital | 8 PDF + 2 docx lus **sans OCR, en millisecondes** (2026-06-26). Sert aussi de dénominateur de complétude (`page_count`) et de moteur de rendu. |
| **RapidOCR** | ✅ **Gardé** — lane API temps réel | 3,7–20,7 s/image CPU. Seule voie tenable en secondes. Pip-only, aucune dép système. |
| **python-docx** | ✅ **Gardé** | Lecture `.docx` native. Les docx sont volontairement non-structurés → lane RAG. |
| **Docling** (pipeline standard) | ✅ **Gardé** — lane batch / RAG | Apporte layout + ordre de lecture + tables. ⚠️ **Lourd même sur du petit** : 14–77 s pour un document d'**une page** (2026-07-21). ⚠️ **Perd des pages en silence** sous contention mémoire en rapportant `EXCELLENT` → d'où `conversion_guard`. ⚠️ `document.pages` **ment** (garde l'entrée d'une page dont l'OCR a planté) : le signal honnête est `confidence.pages`. |
| **LightOnOCR-2-1B** | ✅ **Gardé** — escalade / cas durs | Qualité **parfaite** sur photos d'écran où Tesseract=bruit et granite=poubelle. RAM ~1,8 Go. **~171 s/image CPU → batch uniquement, jamais l'API.** Préféré aussi pour le RGPD (éditeur français). |
| **Tesseract** | ❌ **Écarté** (2026-06-26) | Binaire **absent** de la machine cible, et sortie = **bruit pur** sur les images dures du corpus. RapidOCR fait mieux sans dépendance système. |
| **granite-docling-258M** (VLM) | ❌ **Écarté** (2026-06-28) | **2051 s** au premier essai, **307 s/image** ensuite, sortie poubelle (`0 0 0…` en boucle, « Screenshot »). Modèle de *doc-conversion*, mauvais outil pour des photos d'écran. **Finding transférable : ≤1B borne la TAILLE, pas la LATENCE CPU** → tout VLM-OCR est batch-only. |
| **Docling — pipeline VLM** | ❌ **Non utilisé** | C'est le chemin qui héberge granite-docling (`pipeline_cls=VlmPipeline`). Écarté avec lui. On tourne le **pipeline standard**, jamais celui-là. |
| **markitdown — OCR** | ❌ **Exclu RGPD** (2026-07-20) | Son OCR est **cloud uniquement** (plugin GPT-4o / Azure Content Understanding). On traite de la PII en local → rédhibitoire. Le reste de markitdown est local (voir ci-dessous). |

## Structure / corroboration

| Outil | Verdict | Pourquoi (mesuré) |
|---|---|---|
| **markitdown** (born-digital) | 🟡 **Resserré aux TABLES** (2026-07-21) | Sans cloud il est **rapide et 100 % local** (`pdfminer` + `pdfplumber` ; Azure n'est **pas** requis — les convertisseurs Azure ne s'enregistrent que si l'appelant fournit un endpoint). Mais : **0 titre markdown sur 24 PDF réels** → aucune hiérarchie à corroborer ; **frontières de page non fiables** (un doc rendu en **1 page pour ~100 k caractères**). Reste utile pour ses **tables** (via pdfplumber). |
| **pdfplumber** (tables, en direct) | 🟡 **En cours d'adjudication** | Appelé directement plutôt que via markitdown : mêmes tables, mais **par page avec bbox**, ce qui contourne le bug de séparateurs. Méthode **géométrique** (traits de cadre, positions de mots) — donc réellement indépendante du TableFormer **neural** de Docling. |
| **`ftfy`** | ✅ **Gardé** — intégrité caractères | Détecte et **renverse** le mojibake, et **explique** les étapes d'octets. Finding verrouillé : `is_bad()` renvoie **False** sur `U+FFFD` → le check de perte doit être séparé, il n'est pas redondant. |

## Métriques essayées (aussi des outils)

| Approche | Verdict | Pourquoi |
|---|---|---|
| **TF-IDF / sac-de-mots** comme métrique de divergence entre 2 lecteurs | ❌ **Rejeté avant implémentation** | **Invariant à l'ordre par construction.** Les deux lecteurs tirent les mêmes mots de la même couche texte → score ~1.0 quoi qu'il arrive. Il ne peut pas détecter une divergence de **linéarisation**, qui est précisément la panne cherchée. |
| **Comparaison de FORME de tables** (lignes×colonnes) entre Docling et pdfplumber | ❌ **Invalidé par le run réel** (2026-07-21) | **100 % de divergence sur 4 documents.** Motif systématique : Docling trouve **peu de grandes** tables, pdfplumber **beaucoup de petites**. Les deux ne sont pas en désaccord sur la *qualité* mais sur **ce qui constitue UNE table** — une convention de segmentation. Un détecteur qui se déclenche partout ne détecte rien. **Leçon** : le smoke était vert (6/6) parce qu'il figeait *l'hypothèse de conception* (les deux côtés fabriqués avec la même segmentation). Des tests verts ne valident pas une conception — seul le run réel le fait. |
| **Adjudication visuelle** (page rendue à côté des 2 reconstructions) | 🟡 **En cours** | La vérité n'est pas dérivable de deux extracteurs qui se contredisent : il faut une **référence humaine**, même minuscule. Artefact : `table_adjudication_build.py` → HTML local gitignoré. |

## Infrastructure / serving

| Outil | Verdict | Pourquoi |
|---|---|---|
| **llama.cpp + llama-swap** | ✅ **Gardé** | « Le plus rapide et contrôlable ». Le code ne dépend que d'un endpoint compatible OpenAI → le serving est un adaptateur remplaçable. |
| **Ollama** | ❌ **Écarté** | « Trop peu de contrôle », plus une re-validation GBNF/multimodal à repayer. |
| **LocalAI** | ❌ **Écarté** | Même motif de contrôle. |
| **vLLM / TGI** | ❌ **Hors sujet** | Orientés GPU ; la cible est **CPU, ~8 Go, sans GPU**. |

## Leçons transférables (au-delà d'un outil)

1. **≤1B borne la taille, pas la latence CPU.** Un « petit » VLM peut coûter des minutes par image.
2. **Un score de confiance élevé ne dit pas que la lecture est correcte** — un verso illisible a scoré 0,93. Le vrai signal était le checksum MRZ.
3. **Un composite dilue.** Le `mean_grade` de Docling noie le `layout_score` en le moyennant avec un `parse_score` à 1.0 → utiliser le signal brut, pas l'agrégat.
4. **Vérifier qu'un signal est CÂBLÉ avant de s'y fier** : le `table_score` de Docling est `nan` partout dans cette version.
5. **Deux lecteurs du même type partagent leurs angles morts.** Ils lisent la même CMap `ToUnicode` → ils s'accordent sur le même faux. La corroboration n'a de valeur que si les **méthodes** diffèrent (géométrique vs neural), pas seulement les outils.
