# -*- coding: utf-8 -*-
"""
Import .proto -> Capella, via capellambse (pur Python 3, sans Capella
ni Java lances). Traite le fichier cible ET tous ses imports transitifs
(y compris les well-known types Google), en organisant les Classes/
Enumerations/Interfaces dans des sous-packages Capella qui reproduisent
l'arborescence de DOSSIERS de vos fichiers .proto (pas le nom de
fichier individuel : deux .proto dans le meme dossier partagent le
meme package Capella).

Usage :
    python3 import_proto_to_capella.py mon_service.proto /chemin/vers/Model.aird [--layer=la] [--proto-root=DOSSIER] [--strict-types]
    python3 import_proto_to_capella.py mon_dossier_protos/ /chemin/vers/Model.aird [--layer=la] [--strict-types]
        (mode arborescence : tous les .proto sous ce dossier, recursivement --
        --proto-root vaut ce dossier par defaut si non precise)

--layer : oa (Operational Analysis) / sa (System Analysis) /
          la (Logical Architecture, defaut) / pa (Physical Architecture)
--proto-root : dossier racine pour resoudre vos "import" relatifs
          imbriques (ex: import "service_base_api/ServiceB.proto").
          Sans lui, seul le dossier direct du fichier cible est
          utilisable -- OK pour un fichier isole, insuffisant sinon.
--strict-types : n'auto-cree PAS les DataTypes primitifs manquants ;
          arrete l'import si un type primitif necessaire n'existe pas.

Pre-requis modele Capella :
  - Un DataPkg et un InterfacePkg dans la couche choisie (racine sous
    laquelle les sous-packages par dossier sont crees automatiquement)
  - Domaine/groupe/type d'enumeration PVMT deja defini pour le
    streaming (cf. check_pvmt_ready)
  - Par defaut, les DataTypes primitifs manquants sont crees
    automatiquement au besoin -- desactivable avec --strict-types.
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
    PVMT_SOURCE_FILE_KEY,
    PVMT_HEADER_KEY,
)
from setup_primitive_types import ensure_primitive_types


# --------------------------------------------------------------------
# 1. Parsing du/des .proto, multi-fichiers (le fichier cible + tous ses
#    imports transitifs, y compris les "well-known types" Google), avec
#    extraction des commentaires (SourceCodeInfo)
# --------------------------------------------------------------------

def _field_type_ref(field):
    """Reference de type d'un champ : nom qualifie complet pour un
    message/enum (ex: '.service_base_api.Y', utilise comme cle globale
    de resolution inter-fichiers), ou mot-cle primitif proto (ex:
    'int32') sinon."""
    if field.type in (
        descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        descriptor_pb2.FieldDescriptorProto.TYPE_ENUM,
    ):
        return field.type_name  # deja qualifie complet, ex: ".pkg.Type"
    return descriptor_pb2.FieldDescriptorProto.Type.Name(field.type).replace("TYPE_", "").lower()


def _build_comment_index(file_proto):
    """
    Construit un dict {tuple(path): commentaire} a partir de
    SourceCodeInfo. Encodage des chemins (cf. descriptor.proto) :
      [4, i]       -> message_type[i]           (le message lui-meme)
      [4, i, 2, j]  -> message_type[i].field[j]   (un champ)
      [5, i]       -> enum_type[i]                (l'enum lui-meme)
      [5, i, 2, j]  -> enum_type[i].value[j]       (une valeur)
      [6, i]       -> service[i]                 (le service lui-meme)
      [6, i, 2, j]  -> service[i].method[j]        (une methode/rpc)
    """
    index = {}
    for loc in file_proto.source_code_info.location:
        comment = (loc.leading_comments or loc.trailing_comments or "").strip()
        if comment:
            index[tuple(loc.path)] = comment
    return index


def _extract_header_comment(file_proto):
    """
    Le cartouche d'en-tete (licence/copyright) est rattache au champ
    "syntax" de FileDescriptorProto (path [12]), sous deux formes
    possibles selon qu'il y a une ligne vide avant 'syntax = ...;' :
      - PAS de ligne vide (ex: '// Counter interface' juste au-dessus)
        -> leading_comments normal.
      - Ligne vide separatrice (ex: licence Apache multi-lignes)
        -> leading_detached_comments ("detache").
    Les deux cas verifies par test direct."""
    for loc in file_proto.source_code_info.location:
        if list(loc.path) == [12]:
            if loc.leading_comments:
                return loc.leading_comments.strip()
            if loc.leading_detached_comments:
                return "\n\n".join(c.strip() for c in loc.leading_detached_comments).strip()
    return ""


import grpc_tools


def parse_proto_file(proto_path, extra_include_dirs=None, proto_root=None):
    """
    Parse le fichier .proto cible ET tous ses imports transitifs (y
    compris les "well-known types" Google : google/protobuf/empty.proto,
    timestamp.proto, etc. -- inclus automatiquement, sans rien a fournir).

    proto_root : le repertoire racine par rapport auquel vos "import"
    relatifs se resolvent (ex: si votre .proto fait
    `import "service_base_api/ServiceB.proto";`, proto_root doit etre
    le dossier qui CONTIENT "service_base_api/"). Sans lui, seul le
    dossier direct du fichier cible est utilise -- suffisant pour un
    fichier isole, mais PAS pour des imports en chemin relatif imbrique.

    extra_include_dirs : repertoires -I supplementaires additionnels.

    Retourne {"files": [ {path, folder, package, messages, enums,
    services}, ... ]}, un element par fichier reellement compile (cible
    + imports transitifs), dans l'ordre topologique de protoc
    (dependances avant leurs dependants).
    """
    proto_path = os.path.abspath(proto_path)
    well_known_types_dir = os.path.join(os.path.dirname(grpc_tools.__file__), "_proto")

    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "descriptor.pb")
        args = ["protoc"]
        if proto_root:
            args.append("-I" + os.path.abspath(proto_root))
        args.append("-I" + os.path.dirname(proto_path))  # repli pour fichier isole
        args.append("-I" + well_known_types_dir)
        for extra_dir in (extra_include_dirs or []):
            args.append("-I" + os.path.abspath(extra_dir))
        args += ["--include_imports", "--include_source_info",
                  "--descriptor_set_out=" + out_path, proto_path]
        if protoc.main(args) != 0:
            raise RuntimeError("protoc a echoue sur : %s" % proto_path)

        fds = descriptor_pb2.FileDescriptorSet()
        with open(out_path, "rb") as f:
            fds.ParseFromString(f.read())

    files = []
    for file_proto in fds.file:
        comments = _build_comment_index(file_proto)
        folder = os.path.dirname(file_proto.name)  # ex: "service_base_api", "google/protobuf", ou "" (racine)

        messages = []
        for i, msg in enumerate(file_proto.message_type):
            fields = []
            for j, f in enumerate(msg.field):
                fields.append({
                    "name": f.name, "number": f.number, "type": _field_type_ref(f),
                    "repeated": f.label == descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED,
                    "comment": comments.get((4, i, 2, j), ""),
                })
            messages.append({
                "name": msg.name,
                "qualified_name": f".{file_proto.package}.{msg.name}" if file_proto.package else f".{msg.name}",
                "fields": fields,
                "comment": comments.get((4, i), ""),
            })

        # Enums de premier niveau seulement (pas les enums imbriques
        # dans un message -- limite connue, cf. README).
        enums = []
        for i, en in enumerate(file_proto.enum_type):
            values = []
            for j, v in enumerate(en.value):
                values.append({
                    "name": v.name, "number": v.number,
                    "comment": comments.get((5, i, 2, j), ""),
                })
            enums.append({
                "name": en.name,
                "qualified_name": f".{file_proto.package}.{en.name}" if file_proto.package else f".{en.name}",
                "values": values,
                "comment": comments.get((5, i), ""),
            })

        services = []
        for i, svc in enumerate(file_proto.service):
            methods = []
            for j, m in enumerate(svc.method):
                methods.append({
                    "name": m.name,
                    "input_type": m.input_type,   # deja qualifie complet
                    "output_type": m.output_type,  # idem
                    "client_streaming": bool(m.client_streaming),
                    "server_streaming": bool(m.server_streaming),
                    "comment": comments.get((6, i, 2, j), ""),
                })
            services.append({
                "name": svc.name, "methods": methods,
                "comment": comments.get((6, i), ""),
            })

        files.append({
            "path": file_proto.name,
            "folder": folder,
            "package": file_proto.package or None,
            "header": _extract_header_comment(file_proto),
            "messages": messages,
            "enums": enums,
            "services": services,
        })

    return {"files": files}


# --------------------------------------------------------------------
# 2. Resolution des types : primitifs proto -> DataType Capella, et
#    types message/enum -> Class/Enumeration deja creees (n'importe
#    quel fichier, cf. registre global "created_types" indexe par nom
#    qualifie complet)
# --------------------------------------------------------------------

def resolve_type(type_ref, root_data_pkg, created_types):
    """type_ref : soit un mot-cle primitif ('int32', 'string'...), soit
    un nom de type qualifie complet ('.pkg.Message') -- distingue les
    deux par la presence d'un '.' initial (convention protoc).
    root_data_pkg : le DataPkg RACINE de la couche (pas le sous-package
    par dossier) -- les DataTypes primitifs (String, Int32...) sont
    toujours crees/cherches a la racine, partages entre tous les
    modules proto, jamais dupliques par sous-package."""
    if type_ref.startswith("."):
        capella_type = created_types.get(type_ref)
        if capella_type is not None:
            return capella_type
        print("ATTENTION : type '%s' introuvable dans le registre global "
              "(message/enum non cree ?) : le champ/parametre restera "
              "sans type." % type_ref)
        return None
    capella_name = PROTO_TO_CAPELLA_PRIMITIVE.get(type_ref, type_ref)
    try:
        return root_data_pkg.data_types.by_name(capella_name)
    except KeyError:
        print("ATTENTION : type primitif '%s' (-> '%s') introuvable dans "
              "le DataPkg racine, le champ/parametre restera sans type." %
              (type_ref, capella_name))
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


def get_or_create_package_path(root_pkg, folder, cache):
    """Cree/retrouve (find-or-create, idempotent) la hierarchie de
    sous-packages Capella correspondant a un chemin de dossier proto
    (ex: 'service_base_api', ou 'google/protobuf' -> 2 niveaux imbriques).
    folder == "" (fichier a la racine, sans dossier) -> renvoie root_pkg
    tel quel, sans sous-package. Fonctionne identiquement pour un
    DataPkg ou un InterfacePkg (meme API .packages.by_name/.create).
    cache : dict partage entre appels (cle = chemin cumule), pour eviter
    de re-parcourir toute la hierarchie a chaque fichier."""
    if not folder:
        return root_pkg
    if folder in cache:
        return cache[folder]
    current = root_pkg
    accumulated = ""
    for part in folder.split("/"):
        accumulated = f"{accumulated}/{part}" if accumulated else part
        if accumulated in cache:
            current = cache[accumulated]
            continue
        try:
            current = current.packages.by_name(part)
        except KeyError:
            current = current.packages.create(name=part)
        cache[accumulated] = current
    return current


def import_proto_model(proto_model, data_pkg, interface_pkg, model):
    """
    data_pkg / interface_pkg : packages RACINE (ceux de la couche
    choisie). Les messages/enums/interfaces de chaque fichier sont
    places dans un sous-package miroir de son dossier (get_or_create_
    package_path), sous cette racine -- un fichier a la racine (sans
    dossier dans son chemin d'import) va directement dans data_pkg/
    interface_pkg tels quels, sans sous-package supplementaire.
    """
    created_types = {}   # nom qualifie complet -> Class ou Enumeration Capella (TOUS fichiers)
    stats = {"created": 0, "updated": 0}
    data_pkg_cache, interface_pkg_cache = {}, {}
    package_stored = False
    package_pvmt_missing = False
    source_file_stored = False
    source_file_pvmt_missing = False
    header_stored = False
    header_pvmt_missing = False

    def set_source_file(element, path):
        """Pose PVMT_SOURCE_FILE_KEY sur un element (Class/Enumeration/
        Interface), optionnel -- met a jour les compteurs de synthese
        plutot que d'imprimer un avertissement par element."""
        nonlocal source_file_stored, source_file_pvmt_missing
        try:
            element.pvmt[PVMT_SOURCE_FILE_KEY] = path
            source_file_stored = True
        except KeyError:
            source_file_pvmt_missing = True

    def set_header(element, header):
        """Pose PVMT_HEADER_KEY (cartouche licence/copyright), optionnel,
        seulement si le fichier en a effectivement un."""
        nonlocal header_stored, header_pvmt_missing
        if not header:
            return
        try:
            element.pvmt[PVMT_HEADER_KEY] = header
            header_stored = True
        except KeyError:
            header_pvmt_missing = True

    # --- Controle informatif : le chemin de DOSSIER (qui determine le
    #     package Capella, cf. --proto-root) et la declaration "package"
    #     interne au .proto peuvent diverger -- ce n'est jamais bloquant
    #     (le dossier reste seul decisif pour la hierarchie Capella),
    #     mais c'est souvent le signe d'une organisation non
    #     conventionnelle ou d'un fichier importe "a plat" sans son
    #     dossier d'origine (cf. discussion : counter.proto importe tel
    #     quel vs range dans soba_template_api/).
    for file_info in proto_model["files"]:
        if file_info["package"] is None:
            continue
        expected_folder = file_info["package"].replace(".", "/")
        if file_info["folder"] != expected_folder:
            print(f"ATTENTION : '{file_info['path']}' declare "
                  f"'package {file_info['package']};' (attendu comme dossier : "
                  f"'{expected_folder}') mais son chemin de fichier reel est "
                  f"'{file_info['folder'] or '(racine, aucun dossier)'}'  -- le package "
                  f"Capella suivra le CHEMIN DE FICHIER, pas la declaration "
                  f"'package'. Rangez le fichier dans '{expected_folder}/' si "
                  f"vous voulez que les deux coincident.")

    # --- Passe 1 : Enumerations, sur TOUS les fichiers (cible + imports
    #     transitifs, y compris les well-known types Google) ---------
    for file_info in proto_model["files"]:
        target_data_pkg = get_or_create_package_path(data_pkg, file_info["folder"], data_pkg_cache)
        for en in file_info["enums"]:
            capella_enum, is_new = _get_or_create(target_data_pkg.enumerations, en["name"])
            stats["created" if is_new else "updated"] += 1
            if en["comment"]:
                capella_enum.description = en["comment"]
            set_source_file(capella_enum, file_info["path"])
            set_header(capella_enum, file_info["header"])
            for value in en["values"]:
                lit, _ = _get_or_create(capella_enum.owned_literals, value["name"])
                if value["comment"]:
                    lit.description = value["comment"]
            created_types[en["qualified_name"]] = capella_enum

    # --- Passe 2 : Classes (juste creees, sans les champs -- pour que
    #     TOUTES les classes de TOUS les fichiers soient disponibles
    #     avant de resoudre le moindre champ, y compris les references
    #     circulaires entre fichiers) -----------------------------------
    for file_info in proto_model["files"]:
        target_data_pkg = get_or_create_package_path(data_pkg, file_info["folder"], data_pkg_cache)
        for msg in file_info["messages"]:
            capella_class, is_new = _get_or_create(target_data_pkg.classes, msg["name"])
            stats["created" if is_new else "updated"] += 1
            if msg["comment"]:
                capella_class.description = msg["comment"]
            set_source_file(capella_class, file_info["path"])
            set_header(capella_class, file_info["header"])
            created_types[msg["qualified_name"]] = capella_class

    # --- Passe 3 : les champs de chaque Classe, maintenant que le
    #     registre global created_types est complet -----------------
    # known_message_names_by_pkg : uuid du package -> (objet package,
    # set des noms de Classe QU'ON VIENT DE TRAITER pour ce package,
    # union sur tous les fichiers qui y contribuent -- plusieurs
    # fichiers peuvent partager le meme dossier/package).
    known_message_names_by_pkg = {}
    for file_info in proto_model["files"]:
        target_data_pkg = get_or_create_package_path(data_pkg, file_info["folder"], data_pkg_cache)

        for msg in file_info["messages"]:
            capella_class = created_types[msg["qualified_name"]]
            proto_field_names = {f["name"] for f in msg["fields"]}

            for field in msg["fields"]:
                prop, _ = _get_or_create(capella_class.owned_properties, field["name"])
                if field["comment"]:
                    prop.description = field["comment"]
                field_type = resolve_type(field["type"], data_pkg, created_types)
                if field_type is not None:
                    prop.type = field_type

            orphan_fields = [p.name for p in capella_class.owned_properties
                              if p.name not in proto_field_names]
            if orphan_fields:
                print(f"INFO : Class '{msg['name']}' ({file_info['path']}) contient des "
                      f"champs absents de ce .proto (non supprimes) : {', '.join(orphan_fields)}")

        if file_info["messages"]:
            pkg_obj, names = known_message_names_by_pkg.get(target_data_pkg.uuid, (target_data_pkg, set()))
            names.update(m["name"] for m in file_info["messages"])
            known_message_names_by_pkg[target_data_pkg.uuid] = (pkg_obj, names)

    for target_data_pkg, known_names in known_message_names_by_pkg.values():
        orphan_classes = [c.name for c in target_data_pkg.classes if c.name not in known_names]
        if orphan_classes:
            print(f"INFO : le package '{target_data_pkg.name}' contient d'autres Classes "
                  f"non references par cet import : {', '.join(orphan_classes)}")

    # --- Passe 4 : Interfaces/Services, sur TOUS les fichiers (pas
    #     seulement le fichier cible -- un fichier importe qui declare
    #     lui-meme un service voit aussi son Interface creee ; idempotent,
    #     donc reimporter ce fichier plus tard directement ne duplique rien) --
    for file_info in proto_model["files"]:
        target_data_pkg = get_or_create_package_path(data_pkg, file_info["folder"], data_pkg_cache)
        target_interface_pkg = get_or_create_package_path(interface_pkg, file_info["folder"], interface_pkg_cache)

        for svc in file_info["services"]:
            capella_interface, is_new = _get_or_create(target_interface_pkg.interfaces, svc["name"])
            stats["created" if is_new else "updated"] += 1
            if svc["comment"]:
                capella_interface.description = svc["comment"]
            set_source_file(capella_interface, file_info["path"])
            set_header(capella_interface, file_info["header"])

            if file_info["package"]:
                try:
                    capella_interface.pvmt[PVMT_PACKAGE_KEY] = file_info["package"]
                    package_stored = True
                except KeyError:
                    package_pvmt_missing = True

            proto_method_names = {m["name"] for m in svc["methods"]}

            for method in svc["methods"]:
                operation, op_is_new = _get_or_create(
                    capella_interface.owned_features, method["name"], typehint="Service")
                if method["comment"]:
                    operation.description = method["comment"]

                # google.protobuf.Empty (et tout autre type, custom ou
                # Google) est desormais un VRAI type resolu via le
                # registre global -- plus de cas special "pas de
                # Parameter" : Empty est juste un Class avec 0 champ,
                # cree comme n'importe quel autre message importe.
                in_type = resolve_type(method["input_type"], data_pkg, created_types)
                in_param, _ = _get_or_create(operation.parameters, "request", direction="IN")
                if in_type is not None:
                    in_param.type = in_type

                out_type = resolve_type(method["output_type"], data_pkg, created_types)
                out_param, _ = _get_or_create(operation.parameters, "response", direction="OUT")
                if out_type is not None:
                    out_param.type = out_type

                # Streaming -> PVMT, une seule propriete d'enumeration
                mode_name = STREAMING_FLAGS_TO_MODE[(method["client_streaming"], method["server_streaming"])]
                try:
                    literal = get_streaming_mode_literal(model, mode_name)
                    operation.pvmt[PVMT_STREAMING_MODE_KEY] = literal
                except KeyError:
                    print("ATTENTION : domaine/groupe/enumeration PVMT '%s' introuvable "
                          "(ou litteral '%s' absent), streaming non renseigne pour '%s'." %
                          (PVMT_STREAMING_MODE_KEY, mode_name, method["name"]))

            orphan_methods = [f.name for f in capella_interface.owned_features
                               if type(f).__name__ == "Service" and f.name not in proto_method_names]
            if orphan_methods:
                print(f"INFO : Interface '{svc['name']}' contient des operations absentes "
                      f"de ce .proto (non supprimees) : {', '.join(orphan_methods)}")

    if header_stored:
        print(f"INFO : cartouche(s) d'en-tete stocke(s) via PVMT ({PVMT_HEADER_KEY}).")
    if header_pvmt_missing:
        domain_name, group_name = PVMT_HEADER_KEY.split(".")[0:2]
        print(f"INFO : cartouche(s) d'en-tete NON stocke(s) -- le groupe PVMT "
              f"'{domain_name}.{group_name}' n'a pas de propriete 'FileHeader' dans ce "
              f"modele (optionnel, cf. proto_capella_types.py).")
    if source_file_stored:
        print(f"INFO : chemin de fichier d'origine stocke via PVMT ({PVMT_SOURCE_FILE_KEY}) "
              f"sur les Classes/Enumerations/Interfaces -- utilise par --output-root a "
              f"l'export pour regenerer l'emplacement exact.")
    if source_file_pvmt_missing:
        domain_name, group_name = PVMT_SOURCE_FILE_KEY.split(".")[0:2]
        print(f"INFO : chemin de fichier d'origine NON stocke -- le groupe PVMT "
              f"'{domain_name}.{group_name}' n'a pas de propriete 'SourceFile' dans ce "
              f"modele (optionnel, cf. proto_capella_types.py). --output-root utilisera "
              f"alors le nom de l'element Capella comme nom de fichier, pas le nom exact "
              f"d'origine.")

    packages_used = sorted({fi["folder"] for fi in proto_model["files"] if fi["folder"]})
    if packages_used:
        print(f"INFO : packages Capella crees/reutilises (miroir des dossiers proto) : "
              f"{', '.join(packages_used)}")
    any_package_stmt = any(fi["package"] for fi in proto_model["files"])
    if any_package_stmt:
        if package_stored:
            print(f"INFO : package(s) proto stocke(s) via PVMT ({PVMT_PACKAGE_KEY}) sur "
                  f"les Interfaces concernees.")
        if package_pvmt_missing:
            domain_name, group_name = PVMT_PACKAGE_KEY.split(".")[0:2]
            print(f"INFO : package(s) proto NON stocke(s) -- le groupe PVMT "
                  f"'{domain_name}.{group_name}' n'existe pas dans ce modele (optionnel, "
                  f"cf. proto_capella_types.py). Repassez-le a l'export avec --package si besoin.")

    print(f"Bilan : {stats['created']} element(s) cree(s), "
          f"{stats['updated']} element(s) deja existant(s) mis a jour.")
    return created_types


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
    champs de TOUS les fichiers (cible + imports). Les types message/
    enum ne sont pas concernes, ils deviennent des Classes/Enumerations
    creees par l'import lui-meme, sur n'importe quel fichier."""
    names = set()
    for file_info in proto_model["files"]:
        for msg in file_info["messages"]:
            for field in msg["fields"]:
                if field["type"] in PROTO_TO_CAPELLA_PRIMITIVE:
                    names.add(PROTO_TO_CAPELLA_PRIMITIVE[field["type"]])
    return names


def check_types_ready(data_pkg, proto_model):
    """Mode --strict-types : n'auto-cree rien, arrete l'import si un
    type primitif necessaire manque. NB : les types primitifs restent
    tous crees dans le DataPkg RACINE (pas dans les sous-packages par
    dossier), quel que soit le fichier qui les utilise -- ce sont des
    types partages, pas specifiques a un module proto."""
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
par ce .proto (ou ses imports) mais absents du DataPkg : {', '.join(missing)}

Lancez 'python setup_primitive_types.py {{Model.aird}} --layer=...'
pour les creer, ou retirez --strict-types pour laisser l'import les
creer automatiquement.
""")
        return False
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import .proto -> Capella")
    parser.add_argument("proto_path", help="Fichier .proto, OU dossier a parcourir "
                         "recursivement pour importer tous les .proto qu'il contient "
                         "(mode arborescence, detecte automatiquement).")
    parser.add_argument("model_path", help="Chemin vers le fichier .aird du modele Capella")
    parser.add_argument("--layer", default="la", choices=list(LAYER_CHOICES),
                         help="Couche d'architecture cible (defaut: la = Logical Architecture). "
                              "Choix : " + ", ".join(f"{k}={v}" for k, v in LAYER_CHOICES.items()))
    parser.add_argument("--proto-root", default=None,
                         help="Dossier racine par rapport auquel vos directives 'import' "
                              "relatives se resolvent (ex: si vos protos font "
                              "'import \"service_base_api/ServiceB.proto\";', --proto-root "
                              "doit etre le dossier qui CONTIENT service_base_api/). En mode "
                              "arborescence (proto_path est un dossier), --proto-root vaut "
                              "par defaut ce dossier lui-meme si non precise -- c'est "
                              "generalement ce que vous voulez.")
    parser.add_argument("--strict-types", action="store_true",
                         help="N'auto-cree pas les DataTypes primitifs manquants ; "
                              "arrete l'import si l'un d'eux est absent (defaut : "
                              "auto-creation idempotente, avec message informatif).")
    args = parser.parse_args()

    if os.path.isdir(args.proto_path):
        proto_files = []
        for root, _dirs, filenames in os.walk(args.proto_path):
            for fn in sorted(filenames):
                if fn.endswith(".proto"):
                    proto_files.append(os.path.join(root, fn))
        proto_files.sort()
        if not proto_files:
            print(f"ERREUR : aucun fichier .proto trouve sous '{args.proto_path}'.")
            sys.exit(1)
        proto_root = args.proto_root or args.proto_path
        print(f"INFO : mode arborescence -- {len(proto_files)} fichier(s) .proto trouve(s) "
              f"sous '{args.proto_path}' (proto-root='{proto_root}') :")
        for pf in proto_files:
            print(f"    {pf}")
    else:
        proto_files = [args.proto_path]
        proto_root = args.proto_root

    model = capellambse.MelodyModel(args.model_path)

    if not check_pvmt_ready(model):
        sys.exit(1)

    layer = get_layer(model, args.layer)
    data_pkg = layer.data_pkg
    interface_pkg = layer.interface_pkg

    all_created = {}
    total_services = 0
    total_files_processed = 0

    for proto_path in proto_files:
        print(f"\n=== {proto_path} ===")
        proto_model = parse_proto_file(proto_path, proto_root=proto_root)

        if args.strict_types:
            if not check_types_ready(data_pkg, proto_model):
                sys.exit(1)
        else:
            types_created, types_existing = ensure_primitive_types(data_pkg)
            if types_created:
                print(f"INFO : DataTypes primitifs crees automatiquement : "
                      f"{', '.join(types_created)}")

        created = import_proto_model(proto_model, data_pkg, interface_pkg, model)
        all_created.update(created)
        total_services += sum(len(fi["services"]) for fi in proto_model["files"])
        total_files_processed += len(proto_model["files"])

    model.save()
    print("\nImport termine dans %s : %d fichier(s) .proto traite(s) (cibles + imports "
          "transitifs confondus), %d classe(s)/enum(s) au total, %d service(s) crees/mis "
          "a jour." % (LAYER_CHOICES[args.layer], total_files_processed,
                        len(all_created), total_services))