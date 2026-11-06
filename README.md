# Connecteur MCP Alert

Permet à Claude de surveiller **Micromedia ALERT** (télésurveillance / appel
d'astreinte) en temps réel via le **serveur OPC DA intégré à Alert**
(`Alert.OPCServer`), et de relire sa configuration et ses traces.
Aucune licence supplémentaire : le serveur OPC est fourni avec Alert.

Construit sur le même modèle que le connecteur PcVue (`pcvue-mcp`), dont il
reprend le client OPC DA (`opcda.py`). Testé avec Alert 4.1.2 et Python 3.14 (64 bits).

## Outils

| Outil | Rôle |
|---|---|
| `alert_statut` | Santé de la chaîne d'appel : ports, passerelle FX30, drivers VoIP/SMPP, serveurs de données, appels désactivés… avec la liste des **défauts** |
| `alert_alarmes` | Alarmes actives / masquées : acquittement (et par qui), groupe d'appel, dernier appelé, qualité, masquage configuré |
| `alert_astreinte` | Équipe en service pour chaque groupe d'appel, dérogations, état des opérateurs |
| `alert_journal` | Événements des traces (appels, échecs avec cause SIP, acquittements, masquages…), ou résumé chiffré |
| `alert_config` | Configuration du poste **ou d'une sauvegarde `.abf`** : utilisateurs, équipes, groupes d'appel (programme hebdo), variables, etc. |
| `alert_parcourir` | Arborescence OPC d'Alert |
| `alert_lire` | Lecture brute d'items OPC |
| `alert_ecrire` | Écriture d'un item OPC (liste blanche, journal) — **désactivée par défaut** |

## Fichiers

- `server.py` : connecteur MCP
- `opcda.py` : client OPC DA en Python pur (copie de celui de `pcvue-mcp`, messages génériques + `browse_names`)
- `config.json` : configuration
- `journal_ecritures.jsonl` : journal des tentatives d'écriture

## Installation

```
py -m pip install --user mcp
py server.py --test
```

Déclaration dans Claude :

```json
{ "mcpServers": { "alert": { "command": "py", "args": ["C:\\chemin\\vers\\alert-mcp\\server.py"] } } }
```

## Configuration (`config.json`)

| Clé | Rôle |
|---|---|
| `progid` / `process_image` | Serveur OPC (`Alert.OPCServer`) et processus attendu (`Alert.exe`) |
| `station` | Station Alert ; vide = première station trouvée |
| `dossier_alert` | Dossier des données Alert (configuration XML et `trace\`) |
| `masquer_numeros` | Masque le milieu des numéros de téléphone dans les réponses |
| `ecriture_active` / `items_ecriture` | Écriture OPC et liste blanche (`{"item": {"valeurs": [...]}}`) |

## Niveau de droits

Le connecteur doit tourner avec **le même niveau de droits qu'Alert** (les deux
en administrateur, ou aucun des deux). Par sécurité il refuse de se connecter si
`Alert.exe` n'est pas lancé, pour éviter que Windows démarre Alert tout seul.

## Arborescence OPC d'Alert

| Branche | Contenu |
|---|---|
| `System.*` | `LinkFail`, `PortFail`, `ActivePortFail`, `FX30`, `FX30.Fail`, `CallDisabled`, `AlertLock`, `DBStatus`, `Redundancy*`, `TagCount`… |
| `Driver.<driver>.*` | `Name`, `Error`, `LinkFail`, `Status` |
| `Station.<st>.DataServer.<serveur>.*` | `Status`, `LinkFail`, `Disabled`, `TagCount`, `TagFail`… |
| `Station.<st>.TagGroup.<groupe>.*` | `Status`, `Alarm`, `Ack`, `Mask`, `TagCount` |
| `Station.<st>.Group.<groupe>.*` | `Team` (équipe en service), `Derogation`, `DerogTeam`, `Status`, `CallStatus`, `ReliefStatus` |
| `Station.<st>.User.<opérateur>.*` | `Identifier`, `OnDuty`, `ClockIn`, `CallStatus`, `CallFail` |
| `Tag.<serveur>.<variable>.*` | `Value`, `AlarmActive`, `AlarmAck`, `AlarmAckUser`, `AlarmMask`, `Message`, `CallGroup`, `CallStatus`, `LastCallUser`, `LastCallNum`… |

Les dossiers `User`, `Group`, `DataServer` et `TagGroup` refusent `GetItemID` :
les identifiants sont reconstruits (`Station.<st>.Group.<nom>.<propriété>`).
La liste des variables est lue dans `taglist.xml`.

Les codes numériques (`Status`, `LinkStatus`, `CallStatus`, `PortFail`) sont
renvoyés bruts : leur signification n'est pas documentée dans l'aide d'Alert.

## Traces

`alert_journal` lit `trace\tAAMMJJ.log` (UTF-16). Les codes interprétés ont été
établis en comparant les traces aux événements réels :

| Code | Sens |
|---|---|
| 00 | Démarrage d'Alert |
| 04 / 05 | Appel lancé |
| 07 | Appel en échec (essais épuisés) |
| 08 | Erreur d'appel (cause SIP : 408, 503, 407…) |
| 09 / 13 | Apparition d'alarme |
| 12 | Appel acquitté par l'appelé |
| 16 | Passage au groupe de relève |
| 24 | Information |
| 27 / 28 | Alarme masquée / démasquée |
| 33 | Défaut / qualité de variable |

Les autres codes (02, 03, 10, 11, 15, 21, 29…) sont renvoyés sans libellé.

## Sécurité de l'écriture

Les items OPC d'Alert permettent des actions sensibles : acquitter une alarme
(`AlarmAck`), la masquer (`AlarmMask`), **désactiver tous les appels**
(`System.CallDisabled`), poser une dérogation d'astreinte…

- Désactivée par défaut (`"ecriture_active": false`).
- Seuls les items listés dans `items_ecriture` sont modifiables, avec les valeurs autorisées.
- Chaque tentative (réussie, refusée ou en échec) est tracée dans `journal_ecritures.jsonl`.
- L'outil est marqué « destructif » : Claude demande confirmation avant chaque écriture.

Tester d'abord sur un poste de test, jamais directement sur un site en production.

## Pistes

- Interface de commande d'Alert (ligne de commande / DDE : `AckAlarm`, `CallUser`,
  `CallGroup`, `PlayText`…) pour déclencher un appel de test.
- Lecture de l'historique binaire (`histo\hAAMMJJ.log`), dont le format n'est pas documenté.
