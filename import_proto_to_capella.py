# -*- coding: utf-8 -*-
"""
Import .proto -> Capella, via capellambse (pur Python 3, sans Capella
ni Java lances). Chaque appel de creation ci-dessous a ete verifie
reellement contre le modele de demo officiel de capellambse
(tests/data/models/test7_0), pas seulement lu dans la doc.

Usage :
    python3 import_proto_to_capella.py mon_service.proto /chemin/vers/Model.aird

Pre-requis modele Capella :
  - Un DataPkg existant pour recevoir les Classes (messages proto)
  - Un InterfacePkg existant pour recevoir les Interfaces (services proto)
  - Si vous voulez les streaming flags en PVMT : un domaine/groupe PVMT
    deja defini dans le projet (cf. onglet PVMT dans Capella), avec des
    proprietes booleennes pour client/server streaming. Le nom du
    domaine/groupe est a adapter ci-dessous a ce que vous avez defini.
"""

import sys
import capellambse
from grpc_tools import protoc
from google.protobuf import descriptor_pb2
import tempfile
import os


# --------------------------------------------------------------------
# 1. Parsing du .proto (reutilise directement le principe de
#    proto_parse_descriptorset.py -- ici en un seul processus Python3,
#    plus besoin de sous-processus puisqu'on ne tourne plus dans Jython)
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


def parse_proto_file(proto_path):
    proto_path = os.path.abspath(proto_path)
    include_dir = os.path.dirname(proto_path)
    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "descriptor.pb")
        args = ["protoc", "-I" + include_dir, "--include_imports",
                "--descriptor_set_out=" + out_path, proto_path]
        if protoc.main(args) != 0:
            raise RuntimeError("protoc a echoue sur : %s" % proto_path)

        fds = descriptor_pb2.FileDescriptorSet()
        with open(out_path, "rb") as f:
            fds.ParseFromString(f.read())

    file_proto = fds.file[-1]  # le fichier demande (les imports sont avant)
    model = {"package": file_proto.package or None, "messages": [], "services": []}

    for msg in file_proto.message_type:
        fields = [
            {"name": f.name, "number": f.number, "type": _field_type_name(f),
             "repeated": f.label == descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED}
            for f in msg.field
        ]
        model["messages"].append({"name": msg.name, "fields": fields})

    for svc in file_proto.service:
        methods = [
            {"name": m.name, "input_type": _short_type(m.input_type),
             "output_type": _short_type(m.output_type),
             "client_streaming": bool(m.client_streaming),
             "server_streaming": bool(m.server_streaming)}
            for m in svc.method
        ]
        model["services"].append({"name": svc.name, "methods": methods})

    return model


# --------------------------------------------------------------------
# 2. Resolution des types primitifs proto -> DataType Capella existant
#    Le mapping vit desormais dans proto_capella_types.py (partage
#    avec le script d'export) -- modifiez-le LA-BAS, pas ici.
# --------------------------------------------------------------------

from proto_capella_types import (
    PROTO_TO_CAPELLA_PRIMITIVE,
    PVMT_CLIENT_STREAMING_KEY,
    PVMT_SERVER_STREAMING_KEY,
)


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
        created_classes[msg["name"]] = capella_class

    # deuxieme passe : les champs, une fois que toutes les Classes
    # existent (pour resoudre les references de type message a message)
    for msg in proto_model["messages"]:
        capella_class = created_classes[msg["name"]]
        for field in msg["fields"]:
            prop = capella_class.owned_properties.create(name=field["name"])
            field_type = resolve_type(field["type"], data_pkg, created_classes)
            if field_type is not None:
                prop.type = field_type

    # Services -> Interfaces, rpc -> Service (Operation), params
    for svc in proto_model["services"]:
        capella_interface = interface_pkg.interfaces.create(name=svc["name"])

        for method in svc["methods"]:
            operation = capella_interface.owned_features.create("Service", name=method["name"])

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
    Controle prealable, en debut de script : si le domaine/groupe PVMT
    pour le streaming n'existe pas, on arrete tout de suite avec des
    instructions claires, plutot que de laisser tourner un import a
    moitie renseigne (silencieusement, warning par warning).

    NB : capellambse ne permet PAS de creer un domaine/groupe PVMT par
    script (NotImplementedError: "Cannot mutate lists with 'alternate'
    set") -- c'est une restriction volontaire de la bibliotheque. La
    creation reste donc une etape manuelle, unique, a faire dans
    Capella (cf. instructions affichees ci-dessous).
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
  4. Scope : applicable aux elements de type Operation

Relancez ce script une fois cette structure creee.
""")
        return False


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 import_proto_to_capella.py <fichier.proto> <Model.aird>")
        sys.exit(1)

    proto_path, model_path = sys.argv[1], sys.argv[2]

    proto_model = parse_proto_file(proto_path)
    model = capellambse.MelodyModel(model_path)

    if not check_pvmt_ready(model):
        sys.exit(1)

    # A adapter : selection du DataPkg / InterfacePkg cibles dans VOTRE
    # projet. Capella cree un DataPkg "Data" dans CHAQUE couche (OA, SA,
    # LA, PA), donc recherche par nom seul est ambigue -- on cible une
    # couche explicitement. Pour un import d'interfaces logicielles,
    # la Logical Architecture (model.la) ou Physical Architecture
    # (model.pa) est generalement le bon choix, pas l'Operational
    # Analysis (model.oa) qui sert a la couche metier/besoins.
    data_pkg = model.la.data_pkg
    interface_pkg = model.la.interface_pkg

    created = import_proto_model(proto_model, data_pkg, interface_pkg)
    model.save()
    print("Import termine : %d classes, %d services crees." %
          (len(created), len(proto_model["services"])))