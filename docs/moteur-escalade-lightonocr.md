# Moteur d'escalade — LightOnOCR-2 (note IT)

> Livrable IT. Décrit le moteur OCR d'**escalade** (cas durs) : ce qu'il fait, ce dont il a
> besoin, comment l'IT le branche / le remplace. Le code = `ocr_bifunction/reading/engines/lightonocr_engine.py`.
> Cadrage moteurs → [lecture-moteurs-paysage.md](lecture-moteurs-paysage.md).

## Rôle

`LightOnOcrEngine` est une implémentation du **slot jetable `OcrEngine`** (`recognize(bytes) ->
list[TextLine]`), au même titre que `RapidOcrEngine` (fast-path) et `DoclingOcrEngine`. C'est le
**fallback lourd** : quand RapidOCR ne lit pas un cas dur (ex. une MRZ de CI qui ne parse pas ou
dont un checksum échoue après enhance), on **escalade** vers ce moteur.

**Prouvé** : sur un verso de CI dont RapidOCR ne parvenait pas à parser la MRZ (`read_path=none`),
LightOnOCR-2 récupère la MRZ TD1 → **4/4 check digits ICAO passent** → le cas bascule de
`HUMAIN` à `AUTO`. (Sur les photos d'écran HP : qualité parfaite là où Tesseract = bruit.)

## Contrainte d'usage — NON négociable

- **Batch / escalade UNIQUEMENT, JAMAIS l'API temps réel.** ~**171 s/image** CPU, ~**1,8 Go RAM**
  sur la cible 8 Go sans GPU.
- **File sérialisée** (concurrence 1–2). Lancer N escalades en parallèle → OOM. L'escalade doit
  être *fire-and-forget* asynchrone, le résultat retombe via la BD ; elle ne bloque jamais la page.

## Runtime requis (fourni hors-repo par l'IT)

Les binaires et les GGUF sont **volumineux et gitignorés** — non versionnés. L'IT fournit :

| Composant | Proxy local prouvé | Override |
|---|---|---|
| Binaire CLI multimodal llama.cpp | `llama-mtmd-cli.exe` (build **b9542**) | `LIGHTONOCR_BINARY` |
| Modèle GGUF | `models/LightOnOCR-2-1B-Q8_0.gguf` | `LIGHTONOCR_MODEL` |
| Vision projector (mmproj) | `models/mmproj-LightOnOCR-2-1B-Q8_0.gguf` | `LIGHTONOCR_MMPROJ` |

Précédence de résolution : **argument constructeur > variable d'environnement > défaut**.
Une variante existe (`LightOnOCR-2-1B-ocr-soup-Q8_0.gguf`) — non retenue par défaut.

## Commande exacte exécutée

```bash
llama-mtmd-cli -m <model.gguf> --mmproj <mmproj.gguf> --image <page.png> \
  -p "Transcribe all the text in this image, including the machine-readable zone (MRZ) \
      lines at the bottom. Output the text exactly as printed." \
  -ngl 0 -t 4 --temp 0 -c 4096 -n 1024
```

- `-ngl 0` = CPU. `--temp 0` = déterministe (OCR). `-n 1024` borne la génération.
- **Piège Windows** : `--image` lit la virgule comme séparateur et mange les accents en argv.
  L'engine écrit donc l'entrée dans un **fichier temporaire ASCII** avant l'appel (déjà géré).

## Intégration

- **Drop-in** derrière `OcrEngine` : même interface que RapidOCR. Le routage d'escalade
  (value-check raté → ce moteur) se branchera dans `pipeline.py`.
- **Sortie = texte markdown, PAS de géométrie.** Les `TextLine` portent un bbox synthétique
  (ordre de lecture seulement). → adapté à l'extraction **par contenu** (MRZ lue par motif de
  caractères), PAS aux ancres géométriques des templates recto.
- Le micro-texte illisible (déclarations de fond) peut faire **boucler/halluciner** le VLM ;
  sans impact sur la MRZ. `-n` borne le coût.

## RGPD

LightOn = société française (UE). Préféré pour le traitement de PII (on lit des pièces
d'identité). Tourne **100 % local**, aucun appel réseau.
