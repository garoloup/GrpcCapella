# -*- coding: utf-8 -*-
"""
Mapping partage entre types primitifs .proto et noms de DataType
Capella. Utilise par import_proto_to_capella_v2.py (sens proto ->
Capella) ET export_capella_to_proto.py (sens Capella -> proto), pour
que les deux directions restent forcement coherentes.

A METTRE A JOUR une seule fois ici, une fois les noms reels de vos
DataTypes connus (cf. list_datatypes.py sur votre projet).
"""

PROTO_TO_CAPELLA_PRIMITIVE = {
    "string": "String",
    "bool": "Boolean",
    "int32": "Integer",
    "int64": "Integer",
    "uint32": "Integer",
    "uint64": "Integer",
    "float": "Float",
    "double": "Float",
    "bytes": "String",
}

CAPELLA_TO_PROTO_PRIMITIVE = {v: k for k, v in PROTO_TO_CAPELLA_PRIMITIVE.items()}

# PVMT : idem, un seul endroit a corriger pour les deux sens.
PVMT_CLIENT_STREAMING_KEY = "Grpc.Streaming.ClientStreaming"
PVMT_SERVER_STREAMING_KEY = "Grpc.Streaming.ServerStreaming"
