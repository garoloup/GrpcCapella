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

# PVMT streaming : UNE seule propriete d'enumeration (pas deux
# booleens) -- Domain "Grpc", groupe "GrpcMethod", propriete
# "streaming_mode" de type "grpc_streaming_mode" (4 litteraux definis
# dans le domaine lui-meme). Remplace l'ancien design a 2 booleens
# (Grpc.Streaming.ClientStreaming/ServerStreaming), redondant avec
# celui-ci -- gardez UNE SEULE des deux structures dans votre PVMT
# Capella, pas les deux (cf. discussion : meme information encodee
# deux fois = risque de divergence).
PVMT_STREAMING_DOMAIN = "Grpc"
PVMT_STREAMING_GROUP = "GrpcMethod"
PVMT_STREAMING_PROPERTY = "streaming_mode"
PVMT_STREAMING_MODE_KEY = f"{PVMT_STREAMING_DOMAIN}.{PVMT_STREAMING_GROUP}.{PVMT_STREAMING_PROPERTY}"
PVMT_STREAMING_ENUM_TYPE = "grpc_streaming_mode"

# (client_streaming, server_streaming) -> nom du litteral Capella, et
# son inverse -- doivent correspondre EXACTEMENT aux 4 litteraux definis
# dans votre Domain PVMT (cf. capture d'ecran : UNARY, CLIENT_STREAMING,
# SERVER_STREAMING, BIDIR_STREAMING).
STREAMING_FLAGS_TO_MODE = {
    (False, False): "UNARY",
    (True, False): "CLIENT_STREAMING",
    (False, True): "SERVER_STREAMING",
    (True, True): "BIDIR_STREAMING",
}
STREAMING_MODE_TO_FLAGS = {v: k for k, v in STREAMING_FLAGS_TO_MODE.items()}

# PVMT optionnel (String) pour stocker le "package" .proto d'origine sur
# l'Interface -- contrairement au streaming, ABSENT n'empeche PAS
# l'import (le package reste alors seulement disponible via --package
# a l'export). A creer manuellement dans Capella si vous le voulez :
# meme domaine Grpc, nouveau groupe Metadata, propriete String "Package".
# CONFIRME fonctionnel en usage reel (voir conversation).
PVMT_PACKAGE_KEY = "Grpc.Metadata.Package"

# PVMT optionnel (String) pour stocker le CHEMIN RELATIF exact du
# fichier .proto d'origine (ex: "service_base_api/ServiceB.proto"),
# sur Class/Enumeration/Interface -- meme groupe Metadata que Package,
# nouvelle propriete String "SourceFile". Permet a l'export de
# regenerer le fichier de sortie au bon endroit avec le bon nom (cf.
# --output-root), sans avoir a deviner un nom de fichier a partir du
# seul nom de l'element Capella. Optionnel, meme comportement degrade
# que Package si absent (export retombe sur <InterfaceName>.proto).
PVMT_SOURCE_FILE_KEY = "Grpc.Metadata.SourceFile"

# PVMT optionnel (String) pour stocker le cartouche d'en-tete du
# fichier .proto d'origine (licence/copyright, le bloc de commentaires
# separe de "syntax = ...;" par une ligne vide -- capture via
# leading_detached_comments sur le champ "syntax", path [12]). Meme
# groupe Metadata, propriete String "FileHeader". Regenere en tete du
# fichier exporte s'il est present.
PVMT_HEADER_KEY = "Grpc.Metadata.FileHeader"

# PVMT optionnel (String) : style de commentaire d'origine de chaque
# element ("//", "///", "block", "javadoc", "boxed", "boxed_border" --
# cf. proto_comments.py), pose sur Class/Property/Enumeration/
# EnumerationLiteral/Interface/Service. Meme groupe Metadata. Absent,
# l'export utilise "//" partout (comportement anterieur).
PVMT_COMMENT_STYLE_KEY = "Grpc.Metadata.CommentStyle"