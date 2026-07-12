# Serving SLM sur Linux — décision + checklist de livraison

> **Décision ACTÉE (utilisateur, 2026-07-12).** Le produit sera livré sur des serveurs **Linux**
> (modestes, sans GPU). Le serving des 3 slots SLM reste **llama.cpp (`llama-server`) supervisé par
> llama-swap** — « le plus rapide et le plus contrôlable (perf) ». Alternatives écartées :
> **Ollama** (« no way, trop peu de contrôle » — et re-validation GBNF/multimodal à payer),
> **LocalAI** (« pas génial » — une couche de plus sans gain), vLLM/TGI (pensés GPU, hors sujet).

## Le contrat (ce qui ne change PAS)

Le code Python ne connaît qu'un **endpoint HTTP compatible OpenAI** — le serving est un adaptateur :

- `LLAMA_SWAP_URL` (défaut `http://127.0.0.1:8080`) — la seule variable que la porte et le watchdog lisent.
- 3 clés de modèle (le `config.yaml` de llama-swap, **tracké** dans le repo, est le contrat déployable) :
  - `granite-4.0-h-tiny-Q4_K_M` — génération (lane suggestion, nommage D-c, graphe RAG) via
    `/completion` + **`json_schema` → GBNF** (sortie contrainte, cœur des lanes) et `/v1/chat/completions` ;
  - `granite-embedding-r2` — embeddings RAG via `/v1/embeddings` ;
  - `lightonocr-2-1b` — VLM OCR d'escalade via `/v1/chat/completions` **multimodal** (image base64 + mmproj).
- llama-swap charge/décharge par clé avec **TTL** → jamais deux gros modèles résidents (contrainte 8 Go).

**Hors périmètre llama** : Docling et RapidOCR sont des libs Python in-process (pas de serveur) —
rien à changer pour elles côté serving.

## Checklist Linux — « il faudra changer X et Y »

1. **Binaires** (gitignorés, à provisionner) :
   - `tools/llamacpp/b9542/llama-server.exe` → build/release **linux-x64** de llama.cpp.
     ⚠️ Le multimodal LightOnOCR et le GBNF ont été prouvés sur le build **b9542** : pinner ce
     build, OU re-valider sur plus récent (étape 6).
   - `tools/llama-swap/llama-swap.exe` → release Linux de llama-swap (binaire Go, existe telle quelle).
2. **`tools/llama-swap/config.yaml`** — 4 occurrences Windows à adapter :
   - les 3 lignes `cmd:` : `llama-server.exe` → `llama-server` (chemins déjà relatifs) ;
   - la ligne de lancement en commentaire (idem) ;
   - **leviers hardware du jour J** : `-t` = cœurs PHYSIQUES du serveur cible (jamais les logiques,
     leçon mesurée) ; TTL selon la RAM disponible.
3. **`models/`** (gitignoré, multi-Go) à provisionner sur le serveur : granite-4.0-h-tiny Q4_K_M
   (~4 Go), granite-embedding-r2, LightOnOCR-2-1B + **mmproj Q8** (sans le mmproj, pas de vision).
4. **Service** : unit systemd pour llama-swap (`Restart=always`), bind **127.0.0.1 UNIQUEMENT** —
   `llama-server` n'a PAS d'auth ; seule la porte applicative est exposée.
5. **Env Python** : `LLAMA_SWAP_URL` (+ clés modèle si renommées dans le yaml).
6. **Validation à froid AVANT de brancher le flux** (les 3 slots, ~10 min) :
   - GBNF : `uv run python gbnf_diag.py` (le test « BANANE » — la grammaire contraint bien la sortie) ;
   - un appel `/v1/embeddings` (dim 768 attendue) ;
   - un appel LightOnOCR sur une image de test (le multimodal répond, pas de texte-only silencieux).
7. **Windows-only à NE PAS porter** : `HF_HUB_DISABLE_SYMLINKS=1`, contournements cmd.exe/PATH —
   artefacts de la machine de dev.

## Rappels de latence mesurés (CPU, ne changent pas la doctrine)

- Premier appel après idle = **chargement du modèle** (granite ~100-130 s) → le TTL est un
  compromis RAM/latence ; les lanes SLM sont batch/nightly, jamais le fast-path API.
- LightOnOCR ~171-482 s/img CPU → **escalade/batch uniquement, JAMAIS l'API** (doctrine inchangée).
