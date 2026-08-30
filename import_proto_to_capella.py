# -*- coding: utf-8 -*-
"""
Import .proto -> Capella, via capellambse (pur Python 3, sans Capella
ni Java lances).

Usage :
    python3 import_proto_to_capella.py mon_service.proto /chemin/vers/Model.aird [--layer=la]

--layer : oa (Operational Analysis) / sa (System Analysis) /
          la (Logical Architecture, defaut) / pa (Physical Architecture)

Pre-requis modele Capella :
  - Un DataPkg et un InterfacePkg dans la couche choisie
  - Domaine/groupe PVMT deja defini pour le streaming (cf. check_pvmt_ready)
"""

import sys
import argparse
import capellambse
from grpc_tools import protoc
from google.protobuf import descriptor_pb2
import tempfile
import os

from proto_capella_types import (
    PROTO_TO_CAPELLA_PRIMITIVE,
    PVMT_CLIENT_STREAMING_KEY,
    PVMT_SERVER_STREAMING_KEY,
)


# --------------------------------------------------------------------
# 1. Parsing du .proto, avec extraction des commentaires (SourceCodeInfo)
# --------------------------------------------------------------------

def _field_type_name(field):
    if field.type in (
        descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        descriptor_pb2.FieldDescriptorProto.TYPE_ENUM,
    ):
        return field.type_name.split(".")[-1]
    return descriptor_pb2.FieldDescriptorProto.Type.Name(field.type).replace("TYPE_", "").lower()


def _short_type(full_name):
    return full_name.split(".")[-1] if full_name else full_name


def _build_comment_index(file_proto):
    """
    Construit un dict {tuple(path): commentaire} a partir de
    SourceCodeInfo. Encodage des chemins (cf. descriptor.proto) :
      [4, i]       -> message_type[i]           (le message lui-meme)
      [4, i, 2, j]  -> message_type[i].field[j]   (un champ)
      [6, i]       -> service[i]                 (le service lui-meme)
      [6, i, 2, j]  -> service[i].method[j]        (une methode/rpc)
    """
    index = {}
    for loc in file_proto.source_code_info.location:
        comment = (loc.leading_comments or loc.trailing_comments or "").strip()
        if comment:
            index[tuple(loc.path)] = comment
    return index


def parse_proto_file(proto_path):
    proto_path = os.path.abspath(proto_path)
    include_dir = os.path.dirname(proto_path)
    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "descriptor.pb")
        args = ["protoc", "-I" + include_dir, "--include_imports",
                "--include_source_info", "--descriptor_set_out=" + out_path, proto_path]
        if protoc.main(args) != 0:
            raise RuntimeError("protoc a echoue sur : %s" % proto_path)

        fds = descriptor_pb2.FileDescriptorSet()
        with open(out_path, "rb") as f:
            fds.ParseFromString(f.read())

    file_proto = fds.file[-1]  # le fichier demande (les imports sont avant)
    comments = _build_comment_index(file_proto)
    model = {"package": file_proto.package or None, "messages": [], "services": []}

    for i, msg in enumerate(file_proto.message_type):
        fields = []
        for j, f in enumerate(msg.field):
            fields.append({
                "name": f.name, "number": f.number, "type": _field_type_name(f),
                "repeated": f.label == descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED,
                "comment": comments.get((4, i, 2, j), ""),
            })
        model["messages"].append({
            "name": msg.name, "fields": fields,
            "comment": comments.get((4, i), ""),
        })

    for i, svc in enumerate(file_proto.service):
        methods = []
        for j, m in enumerate(svc.method):
            methods.append({
                "name": m.name, "input_type": _short_type(m.input_type),
                "output_type": _short_type(m.output_type),
                "client_streaming": bool(m.client_streaming),
                "server_streaming": bool(m.server_streaming),
                "comment": comments.get((6, i, 2, j), ""),
            })
        model["services"].append({
            "name": svc.name, "methods": methods,
            "comment": comments.get((6, i), ""),
        })

    return model


# --------------------------------------------------------------------
# 2. Resolution des types primitifs proto -> DataType Capella existant
# --------------------------------------------------------------------

def resolve_type(type_name, data_pkg, created_classes):
    """Cherche d'abord parmi les Classes qu'on vient de creer (types
    message), sinon parmi les DataTypes primitifs existants du DataPkg."""
    if type_name in created_classes:
        return created_classes[type_name]
    capella_name = PROTO_TO_CAPELLA_PRIMITIVE.get(type_name, type_name)
    try:
        return data_pkg.data_types.by_name(capella_name)
    except KeyError:
        print("ATTENTION : type primitif '%s' (-> '%s') introuvable dans "
              "le DataPkg, le champ/parametre restera sans type." %
              (type_name, capella_name))
        return None


# --------------------------------------------------------------------
# 3. Creation des elements Capella
# --------------------------------------------------------------------

def import_proto_model(proto_model, data_pkg, interface_pkg):
    created_classes = {}

    # Messages -> Classes
    for msg in proto_model["messages"]:
        capella_class = data_pkg.classes.create(name=msg["name"])
        if msg["comment"]:
            capella_class.description = msg["comment"]
        created_classes[msg["name"]] = capella_class

    # deuxieme passe : les champs, une fois que toutes les Classes
    # existent (pour resoudre les references de type message a message)
    for msg in proto_model["messages"]:
        capella_class = created_classes[msg["name"]]
        for field in msg["fields"]:
            prop = capella_class.owned_properties.create(name=field["name"])
            if field["comment"]:
                prop.description = field["comment"]
            field_type = resolve_type(field["type"], data_pkg, created_classes)
            if field_type is not None:
                prop.type = field_type

    # Services -> Interfaces, rpc -> Service (Operation), params
    for svc in proto_model["services"]:
        capella_interface = interface_pkg.interfaces.create(name=svc["name"])
        if svc["comment"]:
            capella_interface.description = svc["comment"]

        for method in svc["methods"]:
            operation = capella_interface.owned_features.create("Service", name=method["name"])
            if method["comment"]:
                operation.description = method["comment"]

            in_type = resolve_type(method["input_type"], data_pkg, created_classes)
            out_type = resolve_type(method["output_type"], data_pkg, created_classes)
            if in_type is not None:
                operation.parameters.create(name="request", direction="IN", type=in_type)
            if out_type is not None:
                operation.parameters.create(name="response", direction="OUT", type=out_type)

            # Streaming -> PVMT (necessite que le domaine/groupe existe deja)
            try:
                operation.pvmt[PVMT_CLIENT_STREAMING_KEY] = method["client_streaming"]
                operation.pvmt[PVMT_SERVER_STREAMING_KEY] = method["server_streaming"]
            except KeyError:
                print("ATTENTION : domaine/groupe PVMT '%s' introuvable, "
                      "streaming non renseigne pour '%s'." %
                      (PVMT_CLIENT_STREAMING_KEY, method["name"]))

    return created_classes


# --------------------------------------------------------------------
# 4. Point d'entree
# --------------------------------------------------------------------

def check_pvmt_ready(model):
    """
    Controle prealable : si le domaine/groupe PVMT n'existe pas, on
    arrete tout de suite avec des instructions claires. capellambse ne
    permet pas de le creer par script (limite volontaire de la lib).
    """
    domain_name, group_name = PVMT_CLIENT_STREAMING_KEY.split(".")[0:2]
    try:
        domain = model.pvmt.domains.by_name(domain_name)
        domain.groups.by_name(group_name)
        return True
    except KeyError:
        print(f"""
ARRET : le domaine/groupe PVMT '{domain_name}.{group_name}' n'existe pas
encore dans ce modele. Les flags de streaming gRPC ne pourront pas etre
enregistres tant qu'il n'est pas cree.

A faire UNE FOIS dans Capella (PV Definition Editor) :
  1. Domain          : {domain_name}
  2. Group            : {group_name}
  3. Properties (Boolean) : ClientStreaming, ServerStreaming
  4. Scope : couche(s) ou vivent vos interfaces (Logical/Physical...)

Relancez ce script une fois cette structure creee.
""")
        return False


LAYER_CHOICES = {"oa": "Operational Analysis", "sa": "System Analysis",
                  "la": "Logical Architecture", "pa": "Physical Architecture"}


def get_layer(model, layer_code):
    """Recupere la couche (model.oa / model.sa / model.la / model.pa)
    a partir du code court passe en argument."""
    if layer_code not in LAYER_CHOICES:
        raise ValueError(f"--layer doit etre l'un de : {', '.join(LAYER_CHOICES)}")
    return getattr(model, layer_code)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import .proto -> Capella")
    parser.add_argument("proto_file", help="Fichier .proto a importer")
    parser.add_argument("model_path", help="Chemin vers le fichier .aird du modele Capella")
    parser.add_argument("--layer", default="la", choices=list(LAYER_CHOICES),
                         help="Couche d'architecture cible (defaut: la = Logical Architecture). "
                              "Choix : " + ", ".join(f"{k}={v}" for k, v in LAYER_CHOICES.items()))
    args = parser.parse_args()

    proto_model = parse_proto_file(args.proto_file)
    model = capellambse.MelodyModel(args.model_path)

    if not check_pvmt_ready(model):
        sys.exit(1)

    layer = get_layer(model, args.layer)
    data_pkg = layer.data_pkg
    interface_pkg = layer.interface_pkg

    created = import_proto_model(proto_model, data_pkg, interface_pkg)
    model.save()
    print("Import termine dans %s : %d classes, %d services crees." %
          (LAYER_CHOICES[args.layer], len(created), len(proto_model["services"])))