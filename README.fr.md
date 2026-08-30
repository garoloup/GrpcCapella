# Import / Export .proto <-> Capella

*[English version](README.md)*

Outils en ligne de commande pour importer des services gRPC (fichiers
`.proto`) dans un modèle Capella (Interfaces, Classes, Services,
Parameters), et pour exporter des Interfaces Capella vers des
fichiers `.proto`.

Fonctionne entièrement en dehors de Capella (pas de plug-in, pas de
Python4Capella) : les scripts ouvrent et modifient directement le
fichier `.aird`/XMI du modèle via la bibliothèque `capellambse`.


## 1. Installation

Environnement virtuel dédié recommandé (`uv` ou `venv` classique) :

```bash
uv venv capella-grpc-env
source capella-grpc-env/bin/activate      # macOS/Linux
# capella-grpc-env\Scripts\activate       # Windows

uv pip install grpcio-tools capellambse
```

Ou avec `pip` :

```bash
python3 -m venv capella-grpc-env
source capella-grpc-env/bin/activate
pip install grpcio-tools capellambse
```

Pour figer les versions (reproductibilité pour l'équipe) :

```bash
uv pip freeze > requirements.txt     # puis : uv pip install -r requirements.txt
```


## 2. Fichiers du projet

| Fichier | Rôle |
|---|---|
| `proto_capella_types.py` | Mapping partagé types proto ↔ DataType Capella, et clés PVMT du streaming. **À adapter une seule fois** aux noms réels de votre projet (voir §4). |
| `import_proto_to_capella.py` | Importe un `.proto` vers un modèle Capella (Classes, Interfaces, Services, Parameters, commentaires, streaming). |
| `export_capella_to_proto.py` | Exporte une Interface Capella vers un `.proto`. |
| `verify_import.py` | Relit un modèle après import et affiche ce qui a été créé, pour vérification rapide. |
| `list_datatypes.py` | Liste les `DataType` existants dans le `DataPkg` d'une couche, pour renseigner `proto_capella_types.py`. |
| `setup_primitive_types.py` | Crée les 15 `DataType` primitifs manquants (`Int32`/`UInt64`/`String`/...) dans une couche, si absents. Idempotent. Utilisable seul, ou appelé automatiquement par l'import (§4). |
| `route_guide.proto` | Fichier `.proto` d'exemple (officiel gRPC) pour tester : couvre les 4 modes de RPC (simple, streaming client/serveur/bidirectionnel) et des messages imbriqués. |

Gardez tous les fichiers `.py` dans le même dossier : les scripts
s'importent mutuellement (`from proto_capella_types import ...`).


## 3. Utilisation

### Import : `.proto` → Capella

```bash
python import_proto_to_capella.py mon_service.proto Mon_Modele.aird [--layer=la] [--strict-types]
```

- `--layer` : couche cible, `oa` / `sa` / `la` (défaut) / `pa` — respectivement
  Operational Analysis, System Analysis, Logical Architecture, Physical
  Architecture. Pour des interfaces logicielles gRPC, `la` ou `pa` sont
  les choix pertinents (pas `oa`, qui sert au besoin métier).
- `--strict-types` : n'auto-crée pas les `DataType` primitifs manquants ;
  arrête l'import si l'un d'eux est absent, en listant précisément
  lesquels (voir §4 — par défaut, ils sont auto-créés).
- Le script crée : une `Class` par message, une `Property` par champ, une
  `Interface` par service, un `Service` (opération) par `rpc`, avec ses
  `Parameter` d'entrée/sortie typés.
- Les commentaires du `.proto` (au-dessus de chaque message/champ/service/rpc)
  sont repris dans la `description` Capella correspondante.
- Le mode de streaming (`stream` sur la requête et/ou la réponse) est
  enregistré via PVMT (voir §5) sur l'opération.
- **Idempotent** : relancer l'import sur le même `.proto`, ou une
  version modifiée, ne duplique jamais les éléments déjà présents —
  voir §7.
- **Pré-requis dans le modèle**, dans la couche visée : un `DataPkg` et
  un `InterfacePkg` existants (créés par défaut par Capella), ainsi que
  le domaine/groupe PVMT du streaming (§5, toujours manuel — pas
  d'auto-création possible pour PVMT, contrairement aux types §4).

Après l'import : fermez et rouvrez le projet dans Capella (ou
*Refresh*) pour voir les changements — le script modifie le fichier
sur disque, sans passer par l'instance Capella éventuellement ouverte.

Pour vérifier ce qui a été créé sans repasser par l'interface Capella :

```bash
python verify_import.py Mon_Modele.aird NomDuService
```

### Export : Capella → `.proto`

```bash
python export_capella_to_proto.py Mon_Modele.aird NomDuService sortie.proto [--layer=la]
```

- `--layer` : optionnel, sert uniquement à lever l'ambiguïté si une
  Interface du même nom existe dans plusieurs couches (le script
  s'arrête avec une erreur explicite dans ce cas si `--layer` n'est
  pas précisé).
- Régénère `syntax = "proto3";`, les `message` pour chaque type de
  donnée référencé par le service, puis le `service` avec ses `rpc`
  (y compris les `stream` recalculés depuis PVMT), et les commentaires
  depuis les `description` Capella.


## 4. Réglage des types primitifs

`proto_capella_types.py` contient le mapping **bijectif** (1 type
proto ↔ 1 type Capella) entre types primitifs `.proto` (`string`,
`int32`, `uint64`, `sint32`...) et noms de `DataType` Capella
(`String`, `Int32`, `UInt64`, `SInt32`...). Chaque largeur/signe a son
propre type Capella — pas de fusion en un seul `Integer` générique,
pour ne pas perdre d'information au round-trip.

**Par défaut, les `DataType` primitifs manquants sont créés
automatiquement à l'import**, dans le `DataPkg` de la couche ciblée
(idempotent : ne duplique jamais un type déjà présent). Le script
affiche ce qui a été créé :

```
INFO : DataTypes primitifs crees automatiquement : Int32, String
```

Pour trouver les types créés dans Capella : Project Explorer →
`[votre couche]` → `Data` (le `DataPkg`) → vous y verrez `String`,
`Boolean`, `Int32`, `Int64`, `UInt32`, `UInt64`, `SInt32`, `SInt64`,
`Fixed32`, `Fixed64`, `SFixed32`, `SFixed64`, `Float`, `Double`,
`Bytes` (au fur et à mesure qu'ils sont utilisés — pas forcément les
15 d'un coup, seulement ceux réellement référencés par le `.proto`
importé).

Pour désactiver l'auto-création et forcer un contrôle strict (le
script s'arrête si un type nécessaire manque, sans rien créer) :

```bash
python import_proto_to_capella.py mon_service.proto Mon_Modele.aird --strict-types
```

Vous pouvez aussi préparer un modèle à l'avance, sans lancer d'import,
avec le script autonome :

```bash
python list_datatypes.py Mon_Modele.aird             # voir ce qui existe deja
python setup_primitive_types.py Mon_Modele.aird --layer=la   # creer les 15 types
```

Si votre projet utilise déjà des types primitifs sous d'autres noms,
inutile d'auto-créer : ajustez plutôt les valeurs dans
`PROTO_TO_CAPELLA_PRIMITIVE` (dans `proto_capella_types.py`) pour
qu'elles correspondent aux noms existants. Ce fichier est partagé par
l'import et l'export : une seule modification suffit pour les deux
sens.


## 5. Procédure PVMT (streaming gRPC) — à faire une fois par projet

Le mode de streaming (`stream` côté requête et/ou réponse) est
enregistré comme Property Value via l'extension PVMT de Capella. Ce
domaine/groupe doit être créé **manuellement dans Capella** : ni
Python4Capella ni `capellambse` ne permettent de le créer par script
(limite volontaire des deux outils).

**Sans cette étape**, les scripts s'exécutent quand même, mais le
script d'import **s'arrête avant toute modification** avec un message
explicite (contrôle préalable automatique) tant que la structure
n'existe pas.

### Étapes

1. Vérifiez que l'add-on **PVMT** est installé dans Capella
   (`Help > Install New Software`).
2. Sélectionnez un élément du modèle, ouvrez la vue **Property
   Values** (`Window > Show View > Other... > Property Values`).
3. Dans cette vue, ouvrez le **PV Definition Editor**.
4. Créez la structure suivante :

   | Niveau | Nom | Type |
   |---|---|---|
   | Domain | `Grpc` | — |
   | Group (dans le Domain) | `Streaming` | — |
   | Property (dans le Group) | `ClientStreaming` | Boolean |
   | Property (dans le Group) | `ServerStreaming` | Boolean |

5. Définissez le **Scope** du Group sur la ou les couches où vivent vos
   interfaces (typiquement Logical Architecture et/ou Physical
   Architecture — **pas** "Operational", qui désigne la couche
   Operational Analysis et n'a rien à voir avec les opérations
   d'interface).

Référence vidéo (démonstration officielle Thales, création de domaine
à partir de 12:39) :
[Easily enrich Capella models with your domain extensions](https://www.youtube.com/watch?v=ieVmw54YE94)

### Point de vigilance

Un bug connu de Capella (issue GitHub eclipse-capella/capella#2719,
non résolu à ce jour) fait planter la vue *Property Values* en cliquant
directement sur une valeur booléenne pour l'éditer manuellement dans
cette vue. Cela ne concerne que l'édition manuelle : nos scripts
posent les valeurs via l'API `capellambse`, pas via cette vue, et n'y
sont donc pas exposés.

Les noms `Grpc`, `Streaming`, `ClientStreaming`, `ServerStreaming`
sont ceux utilisés par défaut dans `proto_capella_types.py`
(`PVMT_CLIENT_STREAMING_KEY` / `PVMT_SERVER_STREAMING_KEY`). Si vous
utilisez d'autres noms, modifiez ces deux constantes en conséquence.


## 7. Ré-import : mise à jour vs duplication

L'import est **idempotent**, par nom : relancer sur le même `.proto`
(identique ou modifié) ne crée jamais de doublon. Pour chaque Class,
Property, Interface, Service et Parameter, le script cherche d'abord
un élément existant du même nom au même endroit ; s'il le trouve, il
le met à jour (description, type) ; sinon il le crée.

Ce que ça donne concrètement :

| Situation | Comportement |
|---|---|
| Ré-import du même fichier | Aucune duplication, tout est simplement mis à jour. |
| Version modifiée avec ajout (nouveau champ/méthode/message) | Le nouvel élément est ajouté aux existants. |
| Version modifiée avec suppression | L'élément Capella devenu orphelin (absent du nouveau `.proto`) est **signalé** (`INFO : ... non supprimes`) mais **jamais supprimé automatiquement** — une suppression auto pourrait casser une référence ailleurs dans le modèle (un diagramme, par exemple). À vous de le retirer manuellement si besoin. |

Le script signale aussi, à titre indicatif, les Classes du `DataPkg`
qui ne sont pas du tout liées à ce `.proto` (contenu préexistant sans
rapport) — normal si votre `DataPkg` contient d'autres éléments.


## 8. Limites connues

- **Fidélité `repeated`** : l'export détermine `repeated` à partir de
  la cardinalité (`max_card`) de la `Property` Capella. L'import
  actuel ne pose pas cette cardinalité explicitement — un champ
  `repeated` importé puis ré-exporté ressortira donc comme simple. À
  corriger côté import si un round-trip fidèle sur les listes est
  nécessaire.
- **Types primitifs et PVMT** : les types primitifs s'auto-créent
  (§4) ; PVMT reste une configuration manuelle propre à chaque projet
  Capella (§5), à faire une fois.
- **`oneof`, `map<>`** : non gérés par les scripts actuels.
