# -*- coding: utf-8 -*-
"""
Import .proto -> Capella, via capellambse (pur Python 3, sans Capella
ni Java lances).

Usage :
    python3 import_proto_to_capella.py mon_service.proto /chemin/vers/Model.aird [--layer=la] [--strict-types]

--layer : oa (Operational Analysis) / sa (System Analysis) /
          la (Logical Architecture, defaut) / pa (Physical Architecture)
--strict-types : n'auto-cree PAS les DataTypes primitifs manquants ;
          arrete l'import si un type primitif necessaire n'existe pas
          (comportement de secours pour les projets qui veulent
          controler explicitement leurs DataTypes, sans creation
          automatique meme discrete).

Pre-requis modele Capella :
  - Un DataPkg et un InterfacePkg dans la couche choisie
  - Domaine/groupe PVMT deja defini pour le streaming (cf. check_pvmt_ready)
  - Par defaut, les DataTypes primitifs manquants (String/Boolean/
    Int32/... ) sont crees automatiquement au besoin (idempotent, ne
    duplique jamais un type existant) -- desactivable avec --strict-types.
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
    PVMT_STREAMING_MODE_KEY,
    PVMT_STREAMING_DOMAIN,
    PVMT_STREAMING_GROUP,
    PVMT_STREAMING_PROPERTY,
    PVMT_STREAMING_ENUM_TYPE,
    STREAMING_FLAGS_TO_MODE,
    PVMT_PACKAGE_KEY,
)
from setup_primitive_types import ensure_primitive_types


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


import grpc_tools


EMPTY_TYPE_FULL_NAME = ".google.protobuf.Empty"


def _is_empty(full_type_name):
    return full_type_name == EMPTY_TYPE_FULL_NAME


def parse_proto_file(proto_path, extra_include_dirs=None):
    """
    extra_include_dirs : repertoires -I supplementaires, pour resoudre
    les "import" d'autres .proto (vos propres fichiers partages, par
    exemple). Les "well-known types" Google (google/protobuf/empty.proto,
    timestamp.proto, etc.) sont TOUJOURS inclus automatiquement, via le
    dossier embarque par grpcio-tools -- inutile de les fournir.
    """
    proto_path = os.path.abspath(proto_path)
    include_dir = os.path.dirname(proto_path)
    well_known_types_dir = os.path.join(os.path.dirname(grpc_tools.__file__), "_proto")

    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "descriptor.pb")
        args = ["protoc", "-I" + include_dir, "-I" + well_known_types_dir]
        for extra_dir in (extra_include_dirs or []):
            args.append("-I" + os.path.abspath(extra_dir))
        args += ["--include_imports", "--include_source_info",
                  "--descriptor_set_out=" + out_path, proto_path]
        if protoc.main(args) != 0:
            raise RuntimeError("protoc a echoue sur : %s" % proto_path)

        fds = descriptor_pb2.FileDescriptorSet()
        with open(out_path, "rb") as f:
            fds.ParseFromString(f.read())

    file_proto = fds.file[-1]  # le fichier demande (les imports sont avant)
    comments = _build_comment_index(file_proto)
    model = {"package": file_proto.package or None, "messages": [], "enums": [], "services": []}

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

    # Enums de premier niveau (path [5, i] / valeurs [5, i, 2, j] --
    # cf. descriptor.proto : FileDescriptorProto.enum_type = champ 5).
    # NB : les enums IMBRIQUES dans un message (nested) ne sont pas geres
    # ici, seulement les enums declares au niveau du fichier -- limite
    # connue, cf. README.
    for i, en in enumerate(file_proto.enum_type):
        values = []
        for j, v in enumerate(en.value):
            values.append({
                "name": v.name, "number": v.number,
                "comment": comments.get((5, i, 2, j), ""),
            })
        model["enums"].append({
            "name": en.name, "values": values,
            "comment": comments.get((5, i), ""),
        })

    for i, svc in enumerate(file_proto.service):
        methods = []
        for j, m in enumerate(svc.method):
            methods.append({
                "name": m.name,
                "input_type": _short_type(m.input_type),
                "output_type": _short_type(m.output_type),
                "input_is_empty": _is_empty(m.input_type),
                "output_is_empty": _is_empty(m.output_type),
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

def resolve_type(type_name, data_pkg, created_classes, created_enums=None):
    """Cherche d'abord parmi les Classes/Enumerations qu'on vient de
    creer (types message/enum de ce .proto), sinon parmi les DataTypes
    primitifs existants du DataPkg."""
    if type_name in created_classes:
        return created_classes[type_name]
    if created_enums and type_name in created_enums:
        return created_enums[type_name]
    capella_name = PROTO_TO_CAPELLA_PRIMITIVE.get(type_name, type_name)
    try:
        return data_pkg.data_types.by_name(capella_name)
    except KeyError:
        print("ATTENTION : type '%s' (-> '%s') introuvable (ni message/enum "
              "de ce .proto, ni DataType primitif) : le champ/parametre "
              "restera sans type." % (type_name, capella_name))
        return None


def get_streaming_mode_literal(model, mode_name):
    """Recupere l'objet EnumerationPropertyLiteral pour un nom de mode
    (ex: 'BIDIR_STREAMING') -- une propriete PVMT d'enumeration exige
    l'OBJET litteral reel a l'affectation, pas une simple chaine (verifie
    par test direct : assigner une str leve InvalidModificationError)."""
    domain = model.pvmt.domains.by_name(PVMT_STREAMING_DOMAIN)
    enum_type = domain.enumeration_property_types.by_name(PVMT_STREAMING_ENUM_TYPE)
    return enum_type.literals.by_name(mode_name)


# --------------------------------------------------------------------
# 3. Creation des elements Capella
# --------------------------------------------------------------------

def _get_or_create(collection, name, typehint=None, **create_kwargs):
    """Cherche un element par nom dans la collection ; le cree s'il
    n'existe pas encore. Rend l'import IDEMPOTENT : relancer le script
    sur le meme .proto (ou une version modifiee) met a jour les
    elements existants au lieu de les dupliquer."""
    try:
        return collection.by_name(name), False  # (element, cree_maintenant)
    except KeyError:
        if typehint is not None:
            return collection.create(typehint, name=name, **create_kwargs), True
        return collection.create(name=name, **create_kwargs), True


def import_proto_model(proto_model, data_pkg, interface_pkg, model):
    created_classes = {}
    created_enums = {}
    stats = {"created": 0, "updated": 0}
    proto_message_names = {m["name"] for m in proto_model["messages"]}
    package_stored = False
    package_pvmt_missing = False

    # Enums -> Enumeration (+ EnumerationLiteral), AVANT les Classes qui
    # peuvent les referencer dans leurs champs.
    # LIMITE : Capella ne stocke pas de valeur numerique explicite par
    # litteral -- on suppose donc un enum proto3 standard, sequentiel a
    # partir de 0 (le cas courant). Un enum avec des numeros customises/
    # non contigus perdra cette numerotation au re-export (regenere
    # sequentiellement). A affiner si vous en avez besoin.
    for en in proto_model["enums"]:
        capella_enum, is_new = _get_or_create(data_pkg.enumerations, en["name"])
        stats["created" if is_new else "updated"] += 1
        if en["comment"]:
            capella_enum.description = en["comment"]
        for value in en["values"]:
            lit, _ = _get_or_create(capella_enum.owned_literals, value["name"])
            if value["comment"]:
                lit.description = value["comment"]
        created_enums[en["name"]] = capella_enum

    # Messages -> Classes (find-or-create par nom)
    for msg in proto_model["messages"]:
        capella_class, is_new = _get_or_create(data_pkg.classes, msg["name"])
        stats["created" if is_new else "updated"] += 1
        if msg["comment"]:
            capella_class.description = msg["comment"]
        created_classes[msg["name"]] = capella_class

    # deuxieme passe : les champs, une fois que toutes les Classes/Enums
    # existent (pour resoudre les references de type message a message)
    for msg in proto_model["messages"]:
        capella_class = created_classes[msg["name"]]
        proto_field_names = {f["name"] for f in msg["fields"]}

        for field in msg["fields"]:
            prop, _ = _get_or_create(capella_class.owned_properties, field["name"])
            if field["comment"]:
                prop.description = field["comment"]
            field_type = resolve_type(field["type"], data_pkg, created_classes, created_enums)
            if field_type is not None:
                prop.type = field_type

        # Champs presents dans Capella mais plus dans le .proto source :
        # NON supprimes automatiquement (une suppression auto pourrait
        # casser d'autres references dans le modele) -- juste signales,
        # a vous de decider si vous les retirez a la main.
        orphan_fields = [p.name for p in capella_class.owned_properties
                          if p.name not in proto_field_names]
        if orphan_fields:
            print(f"INFO : Class '{msg['name']}' contient des champs absents "
                  f"de ce .proto (non supprimes) : {', '.join(orphan_fields)}")

    # Classes presentes dans Capella (ce DataPkg) mais absentes du .proto
    orphan_classes = [c.name for c in data_pkg.classes
                       if c.name not in proto_message_names and c.name not in created_classes]
    # (orphan_classes n'est qu'indicatif si le DataPkg contient d'autres
    # classes non liees a cet import -- a affiner si vous importez
    # plusieurs .proto dans le meme DataPkg)

    # Services -> Interfaces, rpc -> Service (Operation), params
    for svc in proto_model["services"]:
        capella_interface, is_new = _get_or_create(interface_pkg.interfaces, svc["name"])
        stats["created" if is_new else "updated"] += 1
        if svc["comment"]:
            capella_interface.description = svc["comment"]

        # Package proto -> PVMT (optionnel, n'empeche pas l'import si
        # absent -- contrairement au streaming). Signale une seule fois
        # au niveau du modele (voir plus bas) si le groupe n'existe pas.
        if proto_model["package"]:
            try:
                capella_interface.pvmt[PVMT_PACKAGE_KEY] = proto_model["package"]
                package_stored = True
            except KeyError:
                package_pvmt_missing = True

        proto_method_names = {m["name"] for m in svc["methods"]}

        for method in svc["methods"]:
            operation, op_is_new = _get_or_create(
                capella_interface.owned_features, method["name"], typehint="Service")
            if method["comment"]:
                operation.description = method["comment"]

            # google.protobuf.Empty : PAS de Parameter cree du tout pour
            # ce cote-la (entree et/ou sortie) -- l'absence de Parameter
            # EST l'encodage de "Empty" ; l'export saura le regenerer.
            # Si un Parameter "request"/"response" existe deja d'un import
            # precedent (avant ce fix) alors que le .proto dit maintenant
            # Empty, on le retire pour rester coherent avec la source.
            if method["input_is_empty"]:
                try:
                    operation.parameters.remove(operation.parameters.by_name("request"))
                except KeyError:
                    pass
            else:
                in_type = resolve_type(method["input_type"], data_pkg, created_classes, created_enums)
                in_param, _ = _get_or_create(operation.parameters, "request", direction="IN")
                if in_type is not None:
                    in_param.type = in_type

            if method["output_is_empty"]:
                try:
                    operation.parameters.remove(operation.parameters.by_name("response"))
                except KeyError:
                    pass
            else:
                out_type = resolve_type(method["output_type"], data_pkg, created_classes, created_enums)
                out_param, _ = _get_or_create(operation.parameters, "response", direction="OUT")
                if out_type is not None:
                    out_param.type = out_type

            # Streaming -> PVMT, une seule propriete d'enumeration
            # (necessite que le domaine/groupe/type existent deja)
            mode_name = STREAMING_FLAGS_TO_MODE[(method["client_streaming"], method["server_streaming"])]
            try:
                literal = get_streaming_mode_literal(model, mode_name)
                operation.pvmt[PVMT_STREAMING_MODE_KEY] = literal
            except KeyError:
                print("ATTENTION : domaine/groupe/enumeration PVMT '%s' introuvable "
                      "(ou litteral '%s' absent), streaming non renseigne pour '%s'." %
                      (PVMT_STREAMING_MODE_KEY, mode_name, method["name"]))

        # Operations presentes dans Capella mais plus dans le .proto :
        # signalees, non supprimees (meme logique que pour les champs).
        orphan_methods = [f.name for f in capella_interface.owned_features
                           if type(f).__name__ == "Service" and f.name not in proto_method_names]
        if orphan_methods:
            print(f"INFO : Interface '{svc['name']}' contient des operations absentes "
                  f"de ce .proto (non supprimees) : {', '.join(orphan_methods)}")

    if orphan_classes:
        print(f"INFO : le DataPkg contient d'autres Classes non references "
              f"par ce .proto : {', '.join(orphan_classes)}")
    if proto_model["package"]:
        if package_stored:
            print(f"INFO : package proto '{proto_model['package']}' stocke via PVMT "
                  f"({PVMT_PACKAGE_KEY}) sur l'Interface.")
        if package_pvmt_missing:
            domain_name, group_name = PVMT_PACKAGE_KEY.split(".")[0:2]
            print(f"INFO : package proto '{proto_model['package']}' NON stocke -- le "
                  f"groupe PVMT '{domain_name}.{group_name}' n'existe pas dans ce modele "
                  f"(optionnel, cf. proto_capella_types.py). Repassez-le a l'export avec "
                  f"--package si besoin.")

    print(f"Bilan : {stats['created']} element(s) cree(s), "
          f"{stats['updated']} element(s) deja existant(s) mis a jour.")
    return created_classes


# --------------------------------------------------------------------
# 4. Point d'entree
# --------------------------------------------------------------------

def check_pvmt_ready(model):
    """
    Controle prealable : si le domaine/groupe/type d'enumeration PVMT
    n'existe pas, on arrete tout de suite avec des instructions claires.
    capellambse ne permet pas de le creer par script (limite volontaire
    de la lib).
    """
    try:
        domain = model.pvmt.domains.by_name(PVMT_STREAMING_DOMAIN)
        domain.groups.by_name(PVMT_STREAMING_GROUP)
        domain.enumeration_property_types.by_name(PVMT_STREAMING_ENUM_TYPE)
        return True
    except KeyError:
        print(f"""
ARRET : la structure PVMT '{PVMT_STREAMING_MODE_KEY}' n'existe pas encore
dans ce modele. Le mode de streaming gRPC ne pourra pas etre enregistre
tant qu'elle n'est pas creee.

A faire UNE FOIS dans Capella (PV Definition Editor) :
  1. Domain                : {PVMT_STREAMING_DOMAIN}
  2. Enumeration type       : {PVMT_STREAMING_ENUM_TYPE}, avec 4 litteraux :
                              UNARY, CLIENT_STREAMING, SERVER_STREAMING,
                              BIDIR_STREAMING
  3. Group                  : {PVMT_STREAMING_GROUP}
  4. Property (dans le Group), type {PVMT_STREAMING_ENUM_TYPE} : {PVMT_STREAMING_PROPERTY}
  5. Scope : couche(s) ou vivent vos interfaces (Logical/Physical...)

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


def referenced_primitive_capella_names(proto_model):
    """Noms Capella des types primitifs reellement references par les
    champs de ce .proto (les types message/enum ne sont pas concernes,
    ils deviennent des Classes creees par l'import lui-meme)."""
    names = set()
    for msg in proto_model["messages"]:
        for field in msg["fields"]:
            if field["type"] in PROTO_TO_CAPELLA_PRIMITIVE:
                names.add(PROTO_TO_CAPELLA_PRIMITIVE[field["type"]])
    return names


def check_types_ready(data_pkg, proto_model):
    """Mode --strict-types : n'auto-cree rien, arrete l'import si un
    type primitif necessaire manque."""
    needed = referenced_primitive_capella_names(proto_model)
    missing = []
    for name in sorted(needed):
        try:
            data_pkg.data_types.by_name(name)
        except KeyError:
            missing.append(name)
    if missing:
        print(f"""
ARRET (--strict-types) : les DataTypes primitifs suivants sont requis
par ce .proto mais absents du DataPkg : {', '.join(missing)}

Lancez 'python setup_primitive_types.py {{Model.aird}} --layer=...'
pour les creer, ou retirez --strict-types pour laisser l'import les
creer automatiquement.
""")
        return False
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import .proto -> Capella")
    parser.add_argument("proto_file", help="Fichier .proto a importer")
    parser.add_argument("model_path", help="Chemin vers le fichier .aird du modele Capella")
    parser.add_argument("--layer", default="la", choices=list(LAYER_CHOICES),
                         help="Couche d'architecture cible (defaut: la = Logical Architecture). "
                              "Choix : " + ", ".join(f"{k}={v}" for k, v in LAYER_CHOICES.items()))
    parser.add_argument("--strict-types", action="store_true",
                         help="N'auto-cree pas les DataTypes primitifs manquants ; "
                              "arrete l'import si l'un d'eux est absent (defaut : "
                              "auto-creation idempotente, avec message informatif).")
    args = parser.parse_args()

    proto_model = parse_proto_file(args.proto_file)
    model = capellambse.MelodyModel(args.model_path)

    if not check_pvmt_ready(model):
        sys.exit(1)

    layer = get_layer(model, args.layer)
    data_pkg = layer.data_pkg
    interface_pkg = layer.interface_pkg

    if args.strict_types:
        if not check_types_ready(data_pkg, proto_model):
            sys.exit(1)
    else:
        types_created, types_existing = ensure_primitive_types(data_pkg)
        if types_created:
            print(f"INFO : DataTypes primitifs crees automatiquement : "
                  f"{', '.join(types_created)}")

    created = import_proto_model(proto_model, data_pkg, interface_pkg, model)
    model.save()
    print("Import termine dans %s : %d classes, %d services crees." %
          (LAYER_CHOICES[args.layer], len(created), len(proto_model["services"])))