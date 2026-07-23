# OCR BiFunction

**Poste d'entrée documentaire bi-mode** : lire des documents (cartes d'identité, factures,
attestations…) → catégoriser → **auto-valider le confiant** → **remonter à l'humain le douteux** →
**bloquer le prouvé non conforme** — sous deux régimes (temps réel / batch). La valeur est dans le
**tri par confiance** et la **croissance organique des templates**, pas dans l'OCR (qui est un
adaptateur interchangeable).

> POC Python destiné à être réintégré par une équipe IT : le **contrat qui traverse la frontière =
> les tables + les surfaces de config** ; tout le reste (UI, serveur, stores SQLite) est un
> adaptateur jetable. Voir [docs/contrat-bd-destination.md](docs/contrat-bd-destination.md).

## L'architecture en un schéma

```
                          ┌─────────────────────────────────────────────────┐
 upload (UI / API) ──────►│  PORTE (api_maquette.py)                        │
                          │  1. garde dossier bloqué (block_holder)         │
                          │  2. politique d'exécution (sync / async / nuit) │
                          │  3. admission plafonnée   (capacité, levier)    │
                          └───────┬───────────────────────────┬─────────────┘
                            sync  │                     async │ (spool + row `received`)
                                  ▼                           ▼
                   ┌──────────────────────────┐   ┌───────────────────────────────┐
                   │ ROUTER 2 LANES           │   │ WATCHDOG (worker_watchdog.py) │
                   │ structuré : template D2  │   │ process séparé, 1 job à la    │
                   │  → extraction → checks   │   │ fois : lanes escalation /     │
                   │  → verdict 3 états       │   │ deferred / nightly (--nightly)│
                   │ non structuré : lane RAG │   │ + sweep décisions + passe     │
                   └──────────┬───────────────┘   │ DRAFT (clustering → drafts)   │
                              │                   └───────────────┬───────────────┘
              ┌───────────────┼───────────────┐                   │
              ▼               ▼               ▼                   ▼
        validated/auto   needs_review     rejected         mêmes issues,
        (record en D1)   (doc RETENU,     (non conforme,   écrites en D1
                         revue humaine)   preuve RETENUE)
```

**Une seule couche de traitement, deux régimes.** La porte ET le watchdog font passer un document
par la MÊME fonction pure `intake.handle_document` (`ocr_bifunction/intake.py`) : cœur de routing +
extraction + verdict, check « type déclaré ≠ type reconnu », réaction de non-conformité, et l'unique
mapping record→row. Elle ne persiste rien — chaque régime (porte / worker) est l'adaptateur qui écrit
la row D1. Résultat : la logique métier n'existe qu'une fois, et se teste sur un store en mémoire.

**Les stores (proxies SQLite d'une future BD interne)** — un domaine = un propriétaire :

| Domaine | Table | Rôle |
|---|---|---|
| D1 | `ocr_jobs` | jobs + queue + **record extrait (source de vérité)** |
| D2 | `ocr_templates` | templates + critères de validation + rôles d'attestation |
| D3 | `ocr_reviews` | revue humaine + staging des suggestions de templates |
| D4 | `ocr_execution_policies` | QUAND traiter (sync / async / nuit) par catégorie |
| D5 | `ocr_issuer_registry` | registre curé des organismes émetteurs |
| D6 | `ocr_conformity_policies` | QUE FAIRE d'un non conforme (block / block_holder / flag) |
| — | `ocr_capacity_settings` | leviers infra (plafond sync, débordement) |

## Les modes de fonctionnement

1. **Sync (temps réel)** — traité dans la requête (moteur rapide RapidOCR ; PDF born-digital lus
   par couche texte, sans OCR). Plafonné par `SYNC_CONCURRENCY_LIMIT` : **la porte ne fond jamais,
   elle dégrade** — au-delà du plafond, l'upload bascule en async (`202 pending`) ou en `503`
   selon la config.
2. **Async immédiat** — spool + row `received` (lane `deferred`), drainée en continu par le
   watchdog. C'est aussi la lane d'**escalade** des CI douteuses (re-lecture avec un moteur lourd).
3. **Async nuit** — lane `nightly`, drainée par `worker_watchdog.py --once --nightly` (invocation
   par un ordonnanceur de nuit). La passe de nuit exécute aussi le **DRAFT** : les inconnus accumulés sont clusterisés
   par layout, un template est brouillonné (ancres + champs + checks candidats dérivés des
   extractions), nommé par SLM (opt-in), et stagé en suggestion que l'humain coche et promeut.

Le choix du mode par catégorie + le hint client optionnel (`processing_mode`) sont de la config
(`/policies`), pas du code.

## Le verdict (3 états) et la non-conformité

- **auto** — tout concorde → validé sans humain.
- **review** — « je ne sais pas » (layout inconnu, input illisible, contexte absent) → revue
  humaine, **document retenu et affiché à côté de l'extraction**.
- **rejected** — « je SAIS que c'est non conforme » (clefs recto↔verso divergentes, dates
  incohérentes, code inventé, titulaire ≠ dossier, émetteur hors registre durci, type déclaré ≠
  type reconnu). La machine prouve une **non-conformité** ; la qualification de fraude appartient
  à la compliance. La preuve (checks calculés + document) est retenue pour la revue ; la
  **réaction** est config métier : bloquer l'upload, bloquer le dossier, ou flagger et continuer.

Chaque check est calculé ; le template dit lesquels sont **requis** (compute-all/config-requires)
et peut en **durcir/adoucir** la sévérité — jamais sur un « je ne peux pas savoir » (fail-loud).

## Lancer en local

```bash
uv sync
uv run uvicorn ocr_bifunction.adapters.api_maquette:app          # la porte (http://127.0.0.1:8000)
uv run python -m ocr_bifunction.adapters.worker_watchdog         # le worker (process séparé) ; --once --nightly = passe de nuit ordonnancée
# SLM opt-in (nommage, suggestions, VLM d'escalade) : llama-swap sur 127.0.0.1:8080
#   → cf. docs/deploiement-linux-serving-slm.md
```

Pages locales (adaptateurs jetables, zéro logique métier) : `/` upload · `/review` revue humaine +
non conformes + suggestions · `/policies` politiques d'exécution, conformité, capacité ·
`/registry` registre des organismes.

## Oracle = runs réels (pas de pytest)

La discipline du repo est **smoke-first sur de vrais documents**. Chaque surface a son runner
versionné, exécutable seul :

`flow_smoke` (boucle complète upload→draft→promotion→re-match, 14 checks) · `policy_smoke` (20) ·
`conformity_smoke` (12) · `severity_smoke` (8) · `load_smoke` (porte sous charge, 10) ·
`holder_reference_smoke` (5) · `corroboration_smoke` (7) · `ui_smoke`, `draft_smoke`,
`verdict_check`, `checks_check`, `context_checks_check`, … — corpus synthétiques PII-free ;
les vrais documents (`inputs/`, gitignoré) ne quittent jamais la machine.

## Structure

Un dossier = un concern. Les dépendances descendent : `flow` → `extraction`/`validation` →
`reading` ; `adapters` ne fait que câbler.

```
ocr_bifunction/
  paths.py                # les chemins repo, dérivés UNE fois (fail-loud si le repo bouge)
  llama_transport.py      # transport llama-swap unique — transverse, hors concern
  reading/                # stage ① LIRE : sortir le texte ET sa géométrie
    reader.py             #   contrat OcrEngine/TextLine, couche texte, seuils image
    preprocess.py, conversion_guard.py, text_integrity_guard.py
    resilient_conversion.py, docling_page_range_converter.py   # le chemin lourd
    engines/              #   slots jetables : rapidocr, docling, lightonocr
  extraction/             # stage ② : des lignes lues aux champs nommés
    template.py           #   match + rebuild par géométrie/pattern, provenance des spans
    mrz.py, reconcile.py  #   MRZ (checksums ICAO) + cross-validation CI recto↔verso
    suggestion.py         #   le SLM propose un template pour un structuré inconnu
  validation/             # stage ③ : le verdict
    checks.py             #   registre de checks anti-fraude (ex-validation.py)
    verdict.py, status.py #   les 3 états et leur projection en statuts D1
  flow/                   # le trajet d'un document
    intake.py             #   LA couche de traitement unique (handle_document, pure)
    orchestrator.py, router.py, pipeline.py
  knowledge/              # ce qu'on fait des documents SANS template
    rag.py, generation.py, reference_graph.py, context_assembly.py
    drafting.py, drafting_flow.py, field_naming.py, promotion.py
  storage/                # les tables : store.py + les repositories au-dessus
  governance/             # les surfaces de config qu'un humain possède (D2..D6 + leviers)
  adapters/               # les entrées, jetables par doctrine
    api_maquette.py       #   la porte HTTP + endpoints des surfaces
    worker_watchdog.py    #   le worker async (recover → drain → sweep → draft)
    extract.py, main.py   #   les CLI
proofs/*.py               # les preuves (oracle = runs réels) + les harnais de mesure
templates/*.json          # seed anonymisé des templates (D2 = la source runtime)
ui/*.html                 # pages locales (peaux sur le contrat HTTP)
docs/                     # contrat de destination, dictionnaire métier, notes de déploiement
```

Toolchain : `uv`, `ruff`, Python 3.12. Code et commentaires en anglais ; docs métier en français.

## Licence

[MIT](LICENSE) © 2026 Filipe Parente.
