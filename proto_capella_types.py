# -*- coding: utf-8 -*-
"""
Mapping partage entre types primitifs .proto et noms de DataType
Capella. Utilise par import_proto_to_capella.py (sens proto -> Capella)
ET export_capella_to_proto.py (sens Capella -> proto), pour que les
deux directions restent forcement coherentes.

Mapping BIJECTIF (1 type proto <-> 1 type Capella) : chaque type
entier proto (int32/int64/uint32/uint64/sint32/sint64/fixed32/
fixed64/sfixed32/sfixed64) a sa propre largeur et son propre signe --
ce sont des informations reelles du contrat gRPC (pas juste
cosmetiques), donc on ne les fusionne PAS en un seul "Integer"
generique : ca romprait le round-trip (un uint64 exporte redeviendrait
un int32, silencieusement).

A METTRE A JOUR une seule fois ici, une fois les noms reels de vos
DataTypes connus (cf. list_datatypes.py sur votre projet). Si vous
utilisez volontairement un seul type Integer generique cote Capella
(compromis accepte, fidelite du round-trip sacrifiee), vous pouvez
fusionner les entrees ci-dessous -- mais le faire consciemment, pas
par defaut.
"""

PROTO_TO_CAPELLA_PRIMITIVE = {
    "string": "String",
    "bytes": "Bytes",
    "bool": "Boolean",
    "int32": "Int32",
    "int64": "Int64",
    "uint32": "UInt32",
    "uint64": "UInt64",
    "sint32": "SInt32",
    "sint64": "SInt64",
    "fixed32": "Fixed32",
    "fixed64": "Fixed64",
    "sfixed32": "SFixed32",
    "sfixed64": "SFixed64",
    "float": "Float",
    "double": "Double",
}

CAPELLA_TO_PROTO_PRIMITIVE = {v: k for k, v in PROTO_TO_CAPELLA_PRIMITIVE.items()}
assert len(CAPELLA_TO_PROTO_PRIMITIVE) == len(PROTO_TO_CAPELLA_PRIMITIVE), (
    "PROTO_TO_CAPELLA_PRIMITIVE doit rester bijectif : deux types proto "
    "ne doivent jamais partager le meme nom Capella, sinon l'inversion "
    "pour l'export perd de l'information silencieusement."
)

# Bornes reelles par type entier/flottant, utilisees par
# setup_primitive_types.py pour poser min_value/max_value sur les
# NumericType crees. None = pas de borne fixe (Float/Double).
# Format : (kind, min_value, max_value)
NUMERIC_TYPE_RANGES = {
    "Int32":    ("INTEGER", -2147483648, 2147483647),
    "Int64":    ("INTEGER", -9223372036854775808, 9223372036854775807),
    "UInt32":   ("INTEGER", 0, 4294967295),
    "UInt64":   ("INTEGER", 0, 18446744073709551615),
    "SInt32":   ("INTEGER", -2147483648, 2147483647),
    "SInt64":   ("INTEGER", -9223372036854775808, 9223372036854775807),
    "Fixed32":  ("INTEGER", 0, 4294967295),
    "Fixed64":  ("INTEGER", 0, 18446744073709551615),
    "SFixed32": ("INTEGER", -2147483648, 2147483647),
    "SFixed64": ("INTEGER", -9223372036854775808, 9223372036854775807),
    "Float":    ("FLOAT", None, None),
    "Double":   ("FLOAT", None, None),
}

# PVMT : idem, un seul endroit a corriger pour les deux sens.
PVMT_CLIENT_STREAMING_KEY = "Grpc.Streaming.ClientStreaming"
PVMT_SERVER_STREAMING_KEY = "Grpc.Streaming.ServerStreaming"

# PVMT optionnel (String) pour stocker le "package" .proto d'origine sur
# l'Interface -- contrairement au streaming, ABSENT n'empeche PAS
# l'import (le package reste alors seulement disponible via --package
# a l'export). A creer manuellement dans Capella si vous le voulez :
# meme domaine Grpc, nouveau groupe Metadata, propriete String "Package".
# NON TESTE de bout en bout (aucune propriete String disponible dans le
# modele de demo utilise pour les tests de ce projet) -- a valider une
# premiere fois chez vous une fois la propriete creee.
PVMT_PACKAGE_KEY = "Grpc.Metadata.Package"