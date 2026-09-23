# .proto <-> Capella Import / Export

*[Version française](README.fr.md)*

Command-line tools to import gRPC services (`.proto` files, including
multi-file setups with cross-imports) into a Capella model (Interfaces,
Classes, Enumerations, Services, Parameters), and to export Capella
Interfaces back to `.proto` files.

Runs entirely outside Capella (no plug-in, no Python4Capella): the
scripts open and modify the model's `.aird`/XMI file directly via the
`capellambse` library.


## 1. Installation

A dedicated virtual environment is recommended (`uv` or plain `venv`):

```bash
uv venv capella-grpc-env
source capella-grpc-env/bin/activate      # macOS/Linux
# capella-grpc-env\Scripts\activate       # Windows

uv pip install grpcio-tools capellambse
```

Or with `pip`:

```bash
python3 -m venv capella-grpc-env
source capella-grpc-env/bin/activate
pip install grpcio-tools capellambse
```

To pin versions (reproducibility across the team):

```bash
uv pip freeze > requirements.txt     # then: uv pip install -r requirements.txt
```


## 2. Files in this project

| File | Role |
|---|---|
| `proto_capella_types.py` | Shared mapping between proto types and Capella `DataType`, plus every PVMT key (streaming, package, source file). **Adjust once** to match the real names used in your project (see §6). |
| `import_proto_to_capella.py` | Imports a `.proto` file (and its transitive imports) into a Capella model. |
| `export_capella_to_proto.py` | Exports a Capella Interface to a `.proto` file. |
| `verify_import.py` | Re-reads a model after import and prints what was created, for a quick sanity check. |
| `list_datatypes.py` | Lists the `DataType`s existing in a layer's `DataPkg`, to help fill in `proto_capella_types.py`. |
| `setup_primitive_types.py` | Creates the missing 15 primitive `DataType`s (`Int32`/`UInt64`/`String`/...) in a layer, if absent. Idempotent. Usable standalone, or called automatically by the import script (§6). |

Keep all `.py` files in the same folder: the scripts import each
other (`from proto_capella_types import ...`).


## 3. Usage — Import: `.proto` -> Capella

```bash
python import_proto_to_capella.py my_service.proto My_Model.aird \
    [--layer=la] [--proto-root=FOLDER] [--strict-types]
```

- `--layer`: target layer, `oa` / `sa` / `la` (default) / `pa` —
  Operational Analysis, System Analysis, Logical Architecture, Physical
  Architecture respectively. For gRPC software interfaces, `la` or `pa`
  are the relevant choices (not `oa`, which is for operational/business
  needs).
- `--proto-root`: root folder your **relative, nested** `import`
  directives resolve against (e.g. if your files have
  `import "service_base_api/ServiceB.proto";`, `--proto-root` must be
  the folder that **contains** `service_base_api/`, not that folder
  itself). Required as soon as your `.proto` imports another file
  living in a different folder than its own — see §5 for details.
- `--strict-types`: does not auto-create missing primitive
  `DataType`s; stops the import if one is missing (see §6 — by
  default, silent auto-creation).

What the script processes, across **every** file it encounters (the
target file plus all its transitive imports, including Google's
"well-known types" such as `google/protobuf/empty.proto`):

- A `message` -> a `Class`, with its fields as `Property`.
- A top-level `enum` -> a Capella `Enumeration`, with its values as
  `EnumerationLiteral` (not enums nested inside a message — known
  limitation, §9).
- A `service` -> an `Interface`, with one `Service` (operation) per
  `rpc` and its typed input/output `Parameter`s.
- `.proto` comments (above each message/field/enum/service/rpc) -> the
  matching Capella `description`.
- The streaming mode (`stream` on the request and/or the response) ->
  PVMT (see §7).
- The folder layout of your `.proto` files -> a mirrored hierarchy of
  Capella sub-packages (see §5).

**Idempotent**: re-running the import on the same `.proto`, a modified
version, or even a different file that imports the same module, never
duplicates elements already present — see §8.

**Model prerequisites**, in the target layer: an existing `DataPkg`
and `InterfacePkg` (created by default by Capella), and the streaming
PVMT (§7, always manual — no auto-creation possible for PVMT, unlike
primitive types, §6).

To check what was created without going back into the Capella UI:

```bash
python verify_import.py My_Model.aird ServiceName
```


## 4. Usage — Export: Capella -> `.proto`

```bash
python export_capella_to_proto.py My_Model.aird ServiceName output.proto \
    [--layer=la] [--order=messages-first] [--package=my.pkg]
```

or, to regenerate the folder tree automatically (see §5):

```bash
python export_capella_to_proto.py My_Model.aird ServiceName \
    --output-root=protos [--layer=la] [--order=messages-first] [--package=my.pkg]
```

`output.proto` (an exact path) and `--output-root` (a root folder,
with the tree regenerated automatically underneath) are **mutually
exclusive** — give one or the other, never both, never neither.

- `--layer`: optional, only needed to disambiguate if an Interface
  with the same name exists in several layers (the script stops with
  an explicit error in that case if `--layer` isn't given).
- `--order`: `messages-first` (default, official gRPC convention —
  detailed types first, the service last) or `service-first` (the
  service at the top of the file, a convention some teams use). Both
  orders produce a valid, recompilable `.proto`.
- `--package`: forces the proto `package` written at the top of the
  file. Without this argument, the export tries to read it
  automatically from the Interface's PVMT (§7); if absent on both
  sides, no `package` line is written. An explicit `--package` always
  takes priority over PVMT.

Regenerates: `syntax = "proto3";`, the needed `import` lines
(Google's well-known types detected automatically, see §5), the
optional `package`, the `enum`/`message` blocks referenced
(transitively, including through the fields of the messages
themselves), then the `service` with its `rpc` entries (streaming
recomputed from PVMT), and comments taken from the Capella
`description` fields.


## 5. Capella package organization (mirrors proto folders)

A Capella package (a nested `DataPkg`/`InterfacePkg`) corresponds to a
**folder** of your `.proto` files — not to an individual file, and not
to the `package X.Y;` declaration inside the file (these are two
distinct pieces of information in Protocol Buffers, which happen to
coincide by convention in most well-organized projects, but the
script never conflates them: the **real file path** is what decides).
Two `.proto` files in the same folder share the same Capella package;
their types resolve against each other even if you only directly
import one of the two files (global resolution, across every
transitively imported file).

**`--proto-root` is required** as soon as your imports use nested
relative paths. Concrete example:

```
protos/
  service_base_api/
    ServiceA.proto   # import "service_base_api/ServiceB.proto";
    ServiceB.proto
```

```bash
python import_proto_to_capella.py protos/service_base_api/ServiceA.proto Model.aird \
    --proto-root=protos
```

`--proto-root=protos` must be the folder that **contains**
`service_base_api/` — not `service_base_api/` itself. Without this
argument, `protoc` can only resolve imports relative to the target
file's own directory, which breaks resolution as soon as the
structure is more than one level deep (and dumps every element at the
root of the `DataPkg`/`InterfacePkg`, with no sub-package).

**Google's well-known types** (`google/protobuf/empty.proto`,
`timestamp.proto`, `duration.proto`, `struct.proto`, `any.proto`,
`field_mask.proto`, `wrappers.proto`) are handled just like any other
imported type: they get created as real Capella `Class`es in a
`google/protobuf` package, with their real fields.
`google.protobuf.Empty` is **not** a special case ("no parameter")
anymore: it's just a `Class` with zero fields, like any other. On
export, these specific types are detected automatically (by checking
their Capella package is `google/protobuf`) and referenced through a
real `import "google/protobuf/xxx.proto";`, never redefined inline.

**Mismatch warning**: if a file's real folder doesn't match its
internal `package X.Y;` declaration (expected folder = the package
name with dots replaced by `/`), the import prints an explicit
`ATTENTION` — informational, never blocking. This is often a sign a
file was imported "flat" (without its original folder) by mistake.

**`--output-root` on export** regenerates the folder tree in reverse,
under that root folder:

```bash
python export_capella_to_proto.py Model.aird ServiceA --output-root=protos --layer=pa
# -> protos/service_base_api/ServiceA.proto
```

The file name is chosen, in priority order: the exact original path
if it was recorded via PVMT (`SourceFile`, §7), otherwise
`<InterfaceName>.proto` inside the folder mirroring the Capella
package (approximate if the original `.proto` file's name differed
from the name of the service it contained).

**Known limitation**: a cross-reference to a **custom** type (not a
Google one) living in a *different* Capella package than the exported
Interface's own is, for now, always **redefined inline** in the
output file rather than referenced through a precise `import`
pointing at its exact origin file — even though that origin file may
be known via `SourceFile` (PVMT), this mechanism isn't yet used to
regenerate a real custom cross-file `import`. The generated file stays
valid and self-contained (it works on its own), just not split across
several files the way the original might be.


## 6. Configuring primitive types

`proto_capella_types.py` holds the **bijective** mapping (1 proto type
<-> 1 Capella type) between `.proto` primitive types (`string`,
`int32`, `uint64`, `sint32`...) and Capella `DataType` names
(`String`, `Int32`, `UInt64`, `SInt32`...). Each width/signedness gets
its own Capella type — no merging into a single generic `Integer`, to
avoid losing information on round-trip.

**By default, missing primitive `DataType`s are created automatically
at import time**, in the target layer's **root** `DataPkg` (never in
the per-folder sub-packages — these are shared types, not specific to
a proto module). Idempotent: never duplicates a type that already
exists. The script prints what was created:

```
INFO : DataTypes primitifs crees automatiquement : Int32, String
```

To find the created types in Capella: Project Explorer ->
`[your layer]` -> `Data` -> you'll see them there directly, as they
get used.

To disable auto-creation and enforce a strict check instead (the
script stops if a needed type is missing, without creating anything):

```bash
python import_proto_to_capella.py my_service.proto My_Model.aird --strict-types
```

You can also prepare a model ahead of time, without running an
import, using the standalone script:

```bash
python list_datatypes.py My_Model.aird                       # see what already exists
python setup_primitive_types.py My_Model.aird --layer=la     # create all 15 types
```

If your project already uses primitive types under different names,
there's no need to auto-create anything: adjust the values in
`PROTO_TO_CAPELLA_PRIMITIVE` (inside `proto_capella_types.py`) to
match the existing names instead. This file is shared by both import
and export: a single edit covers both directions.


## 7. PVMT setup — once per project

Three optional-but-useful pieces of information go through Capella's
PVMT extension: the gRPC streaming mode (§7.1, the only one of the
three that blocks the import if missing), the original proto
`package` (§7.2), and the exact source file path (§7.3). None of these
can be created by script: neither Python4Capella nor `capellambse`
support it (a deliberate limitation of both tools) — it's a one-time
manual setup in Capella, via the **PV Definition Editor** (select any
model element, open the **Property Values** view —
`Window > Show View > Other... > Property Values` — then the PV
Definition Editor from that view).

### 7.1 Streaming (required for a complete import)

A **single enumeration property**, not two separate booleans —
directly models the 4 possible gRPC modes:

| Level | Name | Type |
|---|---|---|
| Domain | `Grpc` | — |
| Enumeration type (inside the Domain) | `grpc_streaming_mode` | 4 literals: `UNARY`, `CLIENT_STREAMING`, `SERVER_STREAMING`, `BIDIR_STREAMING` |
| Group (inside the Domain) | `GrpcMethod` | — |
| Property (inside the Group) | `streaming_mode` | type `grpc_streaming_mode` |

Set the Group's Scope to the layer(s) where your interfaces live
(Logical/Physical — not "Operational", which refers to the
Operational Analysis layer and has nothing to do with interface
operations).

**Without this structure**, the import stops **before making any
change**, with an explicit message listing exactly what's missing —
an automatic pre-flight check, not a silent partial import.

The names `Grpc`, `GrpcMethod`, `streaming_mode`, `grpc_streaming_mode`
and the 4 literals are the defaults used in `proto_capella_types.py`
(`PVMT_STREAMING_*`, `STREAMING_FLAGS_TO_MODE`). If you use different
names, update those constants accordingly.

### 7.2 Original package (optional)

| Level | Name | Type |
|---|---|---|
| Domain | `Grpc` (same as above) | — |
| Group (new, inside the Domain) | `Metadata` | — |
| Property (inside Metadata) | `Package` | String |

If present, the original proto `package` (e.g. `soba_template_api`)
is stored automatically on each Interface at import time, and read
back automatically at export (falling back to an explicit `--package`
if missing on either side). **Missing, it does not block the
import** — unlike streaming, this is a convenience, not a
prerequisite.

### 7.3 Original file path (optional)

| Level | Name | Type |
|---|---|---|
| Domain | `Grpc` | — |
| Group | `Metadata` (same as above) | — |
| Property (inside Metadata) | `SourceFile` | String |

If present, records the exact original `.proto` file path (e.g.
`service_base_api/ServiceB.proto`) on every `Class`, `Enumeration` and
`Interface` created. Used by `--output-root` on export (§4, §5) to
regenerate the folder tree with the exact original file names rather
than a name approximated from the Capella element's own name. If
absent, `--output-root` falls back to `<folder>/<InterfaceName>.proto`.


## 8. Re-importing: update vs duplication

The import is **idempotent**, by name, within each Capella package
involved: re-running it on the same `.proto` (identical, modified, or
even a different file that transitively imports it) never creates a
duplicate. For every Class, Property, Enumeration, Interface, Service
and Parameter, the script first looks for an existing element with
the same name in the same place; if found, it updates it (description,
type); otherwise it creates it.

| Situation | Behavior |
|---|---|
| Re-importing the same file | No duplication, everything is simply updated. |
| Modified version with an addition (new field/method/message/enum) | The new element is added alongside the existing ones. |
| Modified version with a removal | The now-orphaned Capella element (missing from the new `.proto`) is **flagged** (`INFO : ... non supprimes`) but **never deleted automatically** — an automatic deletion could break a reference elsewhere in the model (e.g. a diagram). Remove it manually if needed. |
| Importing file B that was already pulled in transitively (via file A) | Finds the already-created elements in the right package, duplicates nothing — no matter which file you "enter" a proto module through, the end result converges. |

The script also flags, for information, any Classes in a package that
aren't related to any of the files processed in this particular
import (pre-existing unrelated content) — expected if your `DataPkg`
(or a sub-package) holds other elements too.


## 9. Known limitations

- **`repeated`**: export determines `repeated` from the Capella
  `Property`'s cardinality (`max_card`). Import does not set this
  cardinality explicitly, so a `repeated` field that's imported and
  re-exported will come back as a plain singular field.
- **Nested enums**: only `enum`s declared at file level are handled,
  not ones nested inside a `message`.
- **Enum numeric values**: Capella has no explicit per-literal value
  field (`EnumerationLiteral` has no `value` attribute) — regenerated
  sequentially from 0 on export. Faithful for a standard proto3 enum
  (the common case), not for custom or gapped numbering.
- **Custom cross-package imports**: see the detailed limitation in §5
  (custom types redefined inline rather than through a precise
  `import` pointing at their origin file).
- **`oneof`, `map<>`**: not handled by the current scripts.