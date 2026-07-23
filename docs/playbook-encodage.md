# Playbook encodage — garantir le texte de bout en bout

> Doc **transverse** (pas propre à ce projet) : s'applique à toute frontière où du texte entre,
> transite ou sort — API, ingestion BD, lecture de documents, export. L'instance *dans ce repo* est
> [`ocr_bifunction/reading/text_integrity_guard.py`](../ocr_bifunction/reading/text_integrity_guard.py) ; le concept
> est dans [dictionnaire-metier.md](dictionnaire-metier.md) (« arête sens »).
> Contenu générique volontairement : aucun détail de stack ou de volumétrie interne.

## Le principe (à ne pas contourner)

**Une garantie est une propriété du PIPELINE, pas le résultat d'une détection après coup.**
On garantit **par construction**, à la frontière. La détection/réparation ne sert qu'à la **dette
déjà en base** — elle ne transforme jamais un pipeline non garanti en pipeline garanti.

Corollaire : si un octet a été perdu à l'entrée, **aucun outil ne le récupère**. Ni `ftfy`, ni un
LLM, ni de l'OCR. D'où la priorité absolue à la porte d'entrée.

## Vocabulaire (la confusion qui coûte cher)

| Terme | Définition | Peut se tromper ? |
|---|---|---|
| **OCR** | image → texte (reconnaissance) | **oui** |
| **Extraction couche texte** | lire les caractères déjà encodés (PDF natif, docx) | non (sauf CMap cassée) |
| **Décodage** | octets → `str` selon un charset | oui (silencieusement) |
| **Mojibake** | octets UTF-8 décodés en latin-1/cp1252 | réversible |
| **Perte** | caractère déjà remplacé par `?` / `U+FFFD` | **irréversible** |

⚠️ **L'OCR n'est JAMAIS la réponse à un problème d'encodage sur du texte déjà numérique** : il n'y a
pas d'image, et rendre `Ã©` en image puis l'OCR-er relit fidèlement `Ã©`. La réparation est un
**inverse d'octets déterministe**, pas de la reconnaissance.

## Le contrat en 5 points (le « bout en bout »)

1. **Entrée** — décoder **explicitement** selon le charset déclaré (`Content-Type`). Charset absent
   ou faux → **détecter** (`charset-normalizer`) *ou* politique stricte : **rejeter / mettre en
   quarantaine**. Jamais deviner-puis-stocker.
2. **Interne** — un seul type canonique : `str` Unicode, normalisé **NFC**
   (`unicodedata.normalize("NFC", texte)`).
3. **Stockage** — colonne **ET** table **ET connexion** dans le même encodage complet (`utf8mb4` côté
   MySQL/MariaDB). **La connexion est le tueur silencieux** : une base correcte + une connexion
   `latin1` produit du double-encodage sans un seul message d'erreur.
4. **Sortie** — encoder en UTF-8 explicitement et **déclarer** le charset.
5. **Porte de validation** — refuser ce qui ne décode pas en strict, plutôt que de stocker du
   douteux. **C'est ce point 5 qui EST la garantie de sortie** ; les 4 autres ne font que l'éviter.

## Détecter (signaux, du plus dur au plus heuristique)

Mesuré sur `ftfy` 6.3.1 :

```python
import ftfy, ftfy.badness

texte.count("�")            # > 0  -> PERTE IRRÉVERSIBLE (flag dur, aucune réparation)
ftfy.badness.is_bad(texte)       # bool -> mojibake réversible détecté
ftfy.badness.badness(texte)      # int  -> score, le nombre calibratable
ftfy.fix_and_explain(texte)      # (texte_réparé, [("encode","latin-1"),("decode","utf-8")])
ftfy.fix_encoding(texte)         # réparation encodage seule
```

**Finding verrouillé (vérifié, contre-intuitif) :** `is_bad()` renvoie **`False`** sur une chaîne
contenant `U+FFFD`. Il ne signale que le mojibake **réversible**. Donc le compte de `U+FFFD` est un
signal **séparé et non redondant** — sans lui, la perte irréversible passe inaperçue.

Décoder en strict est le test le plus dur, et il est gratuit :

```python
octets.decode("utf-8")           # UnicodeDecodeError -> ce n'est pas de l'UTF-8
```

## Réparer — politique

| Cas | Action |
|---|---|
| Propre | passer |
| Mojibake **réversible** | proposer la réparation en **SUGGESTION**, l'humain valide. Jamais d'auto-fix silencieux sur de la donnée stockée. |
| `U+FFFD` / perte | **flag dur**, aucune réparation possible. Remonter à l'humain, tracer. |

## Fabriquer un cas de test (on n'en trouve pas en ligne)

Personne ne publie ses données cassées. **Fabriquer, jamais chercher** — et **jamais depuis un
littéral de source ou de shell** (l'encodage du terminal corrompt l'échantillon avant le test) :

```python
original = "été à Noël"
mojibake = original.encode("utf-8").decode("latin-1")   # -> "Ã©tÃ© Ã  NoÃ«l"
perte    = "abc�def"                               # perte irréversible
```

Pour un test **bout-en-bout** dans un pipeline documentaire : écrire le mojibake **directement dans
la couche texte** d'un PDF de test. Le garde lit la *chaîne extraite* — la cause de la corruption lui
est indifférente, donc inutile de dénicher un PDF à `ToUnicode` cassée.

## Diagnostiquer une base « latin1 vs Unicode »

`latin1_swedish_ci` est la collation **par défaut historique** de MySQL/MariaDB : sa présence signale
une base créée aux réglages d'usine, donc souvent des **octets UTF-8 rangés dans des colonnes
latin1**.

**Distinguer les 3 cas AVANT tout correctif — le remède diffère et se tromper corrompt davantage :**

| Cas | Symptôme | Remède |
|---|---|---|
| **(a)** latin1 réellement latin1 | accents corrects en lecture latin1 | convertir normalement (`CONVERT TO CHARACTER SET`) |
| **(b)** UTF-8 dans colonne latin1 | `LENGTH() > CHAR_LENGTH()`, s'affiche mal côté client UTF-8 | **réinterpréter sans ré-encoder** : `MODIFY … VARBINARY` puis `MODIFY … CHARACTER SET utf8mb4` |
| **(c)** UTF-8 **double-encodé** | `Ã©` réellement STOCKÉ dans la colonne | réparation applicative (`ftfy`) sur les valeurs, puis (a) |

Diagnostic de départ :

```sql
SHOW VARIABLES LIKE 'character_set%';   -- la CONNEXION, pas que la base
SHOW CREATE TABLE <table>;              -- charset colonne + table
SELECT LENGTH(col), CHAR_LENGTH(col), col FROM <table> WHERE col REGEXP 'Ã|Â|â€' LIMIT 20;
```

🚨 **Règles non négociables avant d'exécuter quoi que ce soit :**
- **Backup**, et **rejouer la migration sur une restauration**, jamais d'abord sur la prod.
- **Pas de backfill/migration en Python direct sur la prod** — passer par le mécanisme de migration
  du projet.
- La manip `VARBINARY` ci-dessus est **à revérifier contre la version exacte** du SGBD cible avant
  exécution : elle est citée comme direction, pas comme commande prête à coller.

## Checklist — à dérouler à CHAQUE nouvelle étape

Dès qu'on ajoute une frontière (nouveau client d'API, nouvel import, nouveau lecteur, nouvel export) :

- [ ] Le charset d'entrée est-il **déclaré** ? Sinon, que fait-on — détection ou rejet ?
- [ ] Décode-t-on en **strict** (échec bruyant) plutôt qu'avec `errors="replace"` (perte silencieuse) ?
- [ ] Normalise-t-on en **NFC** avant comparaison/stockage ?
- [ ] La **connexion** au stockage est-elle dans le bon charset (pas seulement la colonne) ?
- [ ] La sortie **déclare**-t-elle son charset ?
- [ ] Y a-t-il une **porte** qui refuse/quarantaine l'indécodable, ou est-ce que ça se stocke quand même ?
- [ ] Un `U+FFFD` peut-il entrer sans lever de drapeau ?

> Un « non » à n'importe laquelle de ces lignes = la garantie de bout en bout est rompue à cet
> endroit précis. Le noter explicitement plutôt que de supposer que ça ira.
