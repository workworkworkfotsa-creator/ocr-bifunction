# Étage ① LIRE — paysage des moteurs & décision (note vivante)

> Recherche datée **2026-06-26**. Sources en bas. À relire avant tout choix figé.
> Le **verdict moteur** se tranche par le **smoke sur les vrais docs de `inputs/`**, pas par cette note.

## Reframe décisif : ① n'est pas « OCR », c'est « sortir le texte du doc »

L'inventaire réel de `inputs/` (8 PDF factures/courriers, ~20 images CI + screenshots + photos
terrain, 2 docx) impose une vérité : **l'OCR n'est qu'UN backend**, pour le contenu *image-only*.

| Type de contenu | Backend adapté | OCR ? |
|---|---|---|
| PDF born-digital (factures, courriers) = couche texte présente | **PyMuPDF** `get_text()` | ❌ non — extraction directe, ~ms, fidélité parfaite |
| PDF scanné / image-only, images (CI, screenshots) | **moteur OCR** (slot interchangeable) | ✅ oui |
| `.docx` | **python-docx** | ❌ non |
| Photo terrain sans texte (MUR/PBO/PM) | OCR → texte ~vide/conf basse | « illisible = signal » → catégorie photo, pas un déchet |

**Lever de perf n°1 pour le volume batch (5000 docs/lot, 8 Go, sans GPU)** : *text-layer-first*.
Le rapport Docling lui-même le dit — « OCR is the most expensive operation ». On n'OCR **que** ce
qui n'a pas de couche texte. → l'interface route **par type + présence de couche texte**, pas
« tout en OCR ».

## Le slot OCR : candidats (à bencher derrière la même interface)

| Moteur | Vitesse CPU | Empreinte | Précision | Confiance native | Verdict 1er tour |
|---|---|---|---|---|---|
| **Tesseract** (pytesseract) | ~0.45 s/page | **~10 Mo** | bonne sur imprimé propre | ✅ par mot | **Défaut de départ** : connu, léger, tient 8 Go/5000 docs, conf native = pile le routing-par-confiance |
| **RapidOCR** | rapide (ONNX) | léger | > Tesseract sur images dures | ✅ | Candidat bench n°1 si Tesseract insuffisant |
| **PaddleOCR** (PP-StructureV3) | moyen | lourd | **le + précis** (94.5% OmniDocBench), layout factures | ✅ | Bench si besoin tables/layout |
| **EasyOCR** | ~3× plus lent | ~500 Mo modèles | bon manuscrit/multi-script | ✅ | Seulement si manuscrit/multi-script |
| **Docling** (lib Python) | 3.1 s/page médian x86, **pics 3–4 Go RAM** | lourd | layout fort | — | ❌ batch 5000 docs = heures + RAM ; pas pour le volume. NB : c'est un *orchestrateur*, pas un modèle |
| **granite-docling-258M** (VLM, GGUF local) | ~3 s/image CPU | Q8 ~178 Mo (+ mmproj F16) | structure/DocTags | — | **Candidat bench** : dispo en local (`Models_gguf/granite-docling-258M-Q8_0.gguf`), tient 8 Go sans GPU. Pour **factures à layout** + **lane escalade API**, PAS le défaut batch (×5000 ≈ 4 h) |
| **Autres VLM/LLM-OCR** (olmOCR-2 8B, dots.ocr 1.7B…) | lent CPU | gros | SOTA | — | ❌ batch à ce volume/hardware (confirme cadrage « VLM 3B mort »), jamais le défaut |

## Décision de départ (révisable par le smoke)

- **Interface `Reader`** (le « jetable ») : `read(path) -> ReadResult{text, confidence, backend, ...}`.
  Routeur par type + couche texte. Trois familles de backends derrière ce slot :
  1. extracteurs couche-texte (PyMuPDF, python-docx) — born-digital, gratuit ;
  2. OCR image (RapidOCR par défaut ; Tesseract/PaddleOCR en bench) ;
  3. VLM parser (granite-docling-258M.gguf) — factures à layout + lane escalade.
- **Backends v0 (slice 1 + 2)** : PyMuPDF (couche texte) + python-docx, puis **RapidOCR** (pip-only,
  décision 2026-06-26 — binaire Tesseract absent de la machine).
- **Bench réservé** : Tesseract / PaddleOCR / granite-docling branchables sans toucher au reste —
  c'est **le but** de l'interface jetable : laisser le smoke sur `inputs/` trancher, sans s'engager.

## Sources (2026-06-26)

- CodeSOTA — PaddleOCR vs Tesseract vs EasyOCR (vitesse/précision 2026)
- CodeSOTA — Best Python OCR Library 2026
- invoicedataextraction.com — Open Source OCR for Invoice Extraction
- Docling Technical Report (arxiv 2408.09869) + Discussions #245/#306 (perf CPU, RAM)
- Spheron — Best Open-Source OCR/Document VLMs to Self-Host 2026
- HuggingFace — granite-docling-258M discussions (lenteur CPU)
