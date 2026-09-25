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
| `proto_comments.py` | Detects and renders comment styles (`//`, `///`, `/* */`, `/** */`, boxed one-`/* */`-per-line, with or without borders), shared by import and export to keep them symmetric (§7.5). |
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

Or, to process an **entire folder tree** of `.proto` files at once
(auto-detected as soon as the first argument is a directory, not a file):

```bash
python import_proto_to_capella.py my_protos_folder/ My_Model.aird [--strict-types]
```

Recursively walks the folder, imports every `.proto` file found (in a
stable order), a single `model.save()` at the end.

**All files are first validated by `protoc`, before any change to the
model.** If one is invalid, `protoc`'s errors (file, line, column) are
shown, the faulty files are listed, and the import stops without touching
the model. Common cause: a type from **another package** used without
qualification. For example, `Point` defined in `route_guide.proto`
(`package routeguide;`) must be written `routeguide.Point`, even when both
files live in the same folder. `--proto-root`
defaults to this folder if not given — generally what you want. Since
the import is idempotent (§8), the order files are processed in
doesn't matter: two files that reference each other converge to the
same result either way.

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

- A `message` -> a `Class`, with its fields as `Property`; a nested
  message -> a nested `Class`.
- A `map<K, V>` -> a nested `Class` `NameEntry` (`key`, `value`) and a
  `0..*` field; `repeated` -> `0..*`; `optional` -> `0..1`; a `oneof`
  field -> PVMT `Oneof` (§7.6).
- A top-level `enum` -> a Capella `Enumeration`, with its values as
  `EnumerationLiteral` (not enums nested inside a message — known
  limitation, §9).
- A `service` -> an `Interface`, with one `Service` (operation) per
  `rpc` and its typed input/output `Parameter`s.
- `.proto` comments (above each message/field/enum/service/rpc) -> the
  matching Capella `description`.
- The file header block (license/copyright, separated from the rest by
  a blank line) -> PVMT, optional (see §7.4).
- The streaming mode (`stream` on the request and/or the response) ->
  PVMT (see §7.1).
- The folder layout of your `.proto` files -> a mirrored hierarchy of
  Capella sub-packages (see §5).

**Idempotent**: re-running the import on the same `.proto`, a modified
version, or even a different file that imports the same module, never
duplicates elements already present — see §8.

**Model prerequisites**, in the target layer: an existing `DataPkg`
and `InterfacePkg` (created by default by Capella), and the streaming
PVMT (§7.1, always manual — no auto-creation possible for PVMT, unlike
primitive types, §6).

To check what was created without going back into the Capella UI:

```bash
python verify_import.py My_Model.aird ServiceName
```


## 4. Usage — Export: Capella -> `.proto`

Export regenerates **complete `.proto` files**, structurally identical to
the originals: each file is rebuilt from the Classes, Enumerations and
Interfaces that came from it (PVMT `SourceFile`, §7.3). This covers files
**without any service** (types only) as well as files with **several
services**.

```bash
# the whole model, folder tree regenerated under protos/
python export_capella_to_proto.py My_Model.aird --all --output-root=protos [--layer=la]

# one specific file, by its original path (useful for a file with no service)
python export_capella_to_proto.py My_Model.aird --file soba_function_api/common.proto --output-root=protos

# the file containing a given service (the WHOLE file, all its services)
python export_capella_to_proto.py My_Model.aird ServiceName --output-root=protos
python export_capella_to_proto.py My_Model.aird ServiceName output.proto
```

One mode at a time: an Interface name, `--file` or `--all`. `--all`
requires `--output-root`; the other modes take either `--output-root` or
an explicit output path.

- `--layer`: restricts to one layer (and disambiguates an Interface name
  that exists in several layers).
- `--order`: `original` (default) reproduces the original declaration
  order if the layout was stored (§7.8), otherwise `messages-first` (enums,
  then messages, then services); `service-first` puts services first.
- `--package`: forces the `package` written. Without it, it is read back
  from PVMT (§7.2). In `--all` mode, leave it blank.

**Interfaces without `SourceFile`** (model Interfaces unrelated to gRPC,
or imported before this mechanism): **skipped by `--all`**, with a message
giving their count. `--include-legacy` exports them the old way (the
service plus the types it references, redefined in
`<InterfaceName>.proto`). When named explicitly, such an Interface is
always exported the old way.

`--all` can be used without `--layer`: elements outside the scope of the
`Metadata` PVMT group (e.g. Operational Analysis classes) are simply
skipped.

Regenerated content: header (§7.4), `syntax`, `import`, `package`, then
the declarations. With the `Layout` property (§7.8), the file is
reproduced **identically** as long as the model has not been changed:
declaration order, file description comment, comment styles, blank lines,
indentation, `option` lines, compact one-line declarations. Without it:
enums, messages, then services, with normalized formatting. Inside messages:

| Proto | Capella representation | Regenerated as |
|---|---|---|
| nested message | nested `Class` (`nested_classes`) | nested `message` |
| `map<K, V> name` | nested `Class` `NameEntry` (`key`, `value`), field `0..*` — `protoc`'s own internal representation | `map<K, V> name` |
| `repeated` | cardinality `0..*` | `repeated` |
| `optional` | cardinality `0..1` | `optional` |
| `oneof group { ... }` | PVMT `Oneof` = `group` on each field (§7.6) | `oneof` block |
| `google.protobuf.Any`, `Empty`, `Timestamp`... | `Class` in the `google/protobuf` package | qualified name + standard `import` |

**Naming and imports**, as in hand-written `.proto` files:

- type from the **same package** -> short name (`TypeA`); from inside a
  message, a nested type is named relatively (`Inner` rather than
  `Features.Inner`). If a short name is shadowed by a nested message of
  the same name, the fully qualified name (`.pkg.Type`) is used;
- type from **another package** -> qualified name (`soba_function_api.TypeA`);
- type defined in **another file** -> `import` of that file: bare file
  name if it lives in the **same folder** (`import "serviceA.proto";`),
  path from the proto root otherwise (`import "soba_function_api/serviceA.proto";`).

Comments are restored in their original style (§7.5): single-line ones at
the end of the code line (column-aligned), multi-line ones above the
element.


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

**Imports between sibling files**: an import written without a folder
(`import "serviceA.proto";` from a file in the same folder) is correctly
attached to that folder's Capella package. The folder is computed from
the file's real path on disk, not from the import name used by
`protoc`. Without this, the types of `serviceA.proto` would be created
twice (at the root of `Data` and in the sub-package).

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
if it was recorded via PVMT (`SourceFile`, §7.3), otherwise
`<InterfaceName>.proto` inside the folder mirroring the Capella
package (approximate if the original `.proto` file's name differed
from the name of the service it contained).

**References between files**: with `SourceFile` configured (§7.3), each
file is regenerated with its own types only, and references those of
other files through a real `import` (same folder or not, same package or
not), with the naming described in §4. Several files exported together
therefore remain compilable as a whole, with no type defined twice.
**Without `SourceFile`**, falls back to the old per-Interface mode (types
redefined inline).


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

Eight optional-but-useful pieces of information go through Capella's
PVMT extension: the gRPC streaming mode (§7.1, the only one of the
eight that blocks the import if missing), the original proto
`package` (§7.2), the exact source file path (§7.3), the file header
block (§7.4), the comment style (§7.5), `oneof` membership (§7.6), field
numbers (§7.7, **strongly recommended**), and the original layout (§7.8). None of these can be created by script: neither
Python4Capella nor `capellambse` support it (a deliberate limitation
of both tools) — it's a one-time manual setup in Capella, via the
**PV Definition Editor** (select any model element, open the
**Property Values** view — `Window > Show View > Other... > Property
Values` — then the PV Definition Editor from that view).

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
than a name approximated from the Capella element's own name, **and**
to generate a real `import` for a cross-reference between two files of
the same package instead of redefining the type inline (§5) —
important in `--all` mode. If absent, `--output-root` falls back to
`<folder>/<InterfaceName>.proto`, and cross-references go back to
being inlined as before.

### 7.4 File header block (optional)

| Level | Name | Type |
|---|---|---|
| Domain | `Grpc` | — |
| Group | `Metadata` (same as above) | — |
| Property (inside Metadata) | `FileHeader` | String |

If present, records the license/copyright header block — everything
before `syntax = "proto3";` — **raw**, markers included (`//`, `/* */`,
`/****/` borders), along with the blank line that may separate it from
`syntax`. It is regenerated character for character on export. If
absent, no header is written on export, even if the original file had
one.

### 7.5 Comment style (optional)

| Level | Name | Type |
|---|---|---|
| Domain | `Grpc` | — |
| Group | `Metadata` (same as above) | — |
| Property (inside Metadata) | `CommentStyle` | String |

`protoc` does not preserve comment syntax, and even degrades the text
(`/// text` becomes `/ text`, the first line of a boxed `/* */` comment
is lost…). The import therefore re-reads each comment from the raw
source file: the Capella **description** gets the clean text (no
markers), and this property records each element's original **style**
(`Class`, `Property`, `Enumeration`, enum value, `Interface`, `Service`):

| Value | Original syntax |
|---|---|
| `//` | `// text` (default) |
| `///` | `/// text` |
| `block` | `/* text ... */`, a single block spanning the lines |
| `javadoc` | `/**` then ` * text` then ` */` |
| `boxed` | one `/* text */` per line, closings aligned |
| `boxed_border` | same, with `/*****/` border lines before and after |

On export, a single-line comment stays at the end of the code line
(column-aligned), in its original style (`// `, `/// ` or `/* */`); a
multi-line comment is restored above the element in its style. If
absent, the export uses `//` everywhere (previous behavior). Atypical
styles (`/*` `*/` markers alone on their line, unindented lines, `//`
without a space…) are restored identically thanks to the raw text stored
in `Layout` (§7.8).


### 7.6 `oneof` groups (optional)

| Level | Name | Type |
|---|---|---|
| Domain | `Grpc` | — |
| Group | `Metadata` (same as above) | — |
| Property (inside Metadata) | `Oneof` | String |

Holds, on each field (`Property`) that belongs to a `oneof`, the group's
name (e.g. `choice`). Export gathers fields sharing that value into a
`oneof choice { ... }` block. The synthetic `oneof`s that `protoc` creates
for `optional` are not involved: `optional` goes through the `0..1`
cardinality. **If absent**, `oneof` fields are exported as plain fields
(valid file, but mutual exclusivity is lost); the import flags it with an
`ATTENTION`.


### 7.7 Field numbers (strongly recommended)

| Level | Name | Type |
|---|---|---|
| Domain | `Grpc` | — |
| Group | `Metadata` (same as above) | — |
| Property (inside Metadata) | `FieldNumber` | String |

Records the original number of each field (`= 5`) and each enum value
(`= 10`). This number, not the name, identifies a field on the gRPC wire:
it must be restored **exactly**, gaps and order included, otherwise
existing clients can no longer decode messages. **If absent**, export
renumbers 1, 2, 3… (0, 1, 2… for an enum), which is only faithful for a
gap-free file; the import flags it with an `ATTENTION`. For elements
imported before this mechanism, export fills in with free numbers, never
creating duplicates.


### 7.8 Original layout (recommended)

| Level | Name | Type |
|---|---|---|
| Domain | `Grpc` | — |
| Group | `Metadata` (same as above) | — |
| Property (inside Metadata) | `Layout` | String |

Technical content (JSON), not meant to be edited in Capella. It holds
everything related to the file's presentation, for a **character-exact**
regeneration:

- **declaration order**, in the file (enums, messages and services
  interleaved) and inside each message (fields, nested messages, `oneof`);
- **the file description comment**, and more generally any comment block
  separated from a declaration by a blank line;
- **the raw text of comments** whose style a standard rendering cannot
  reproduce, their position (above or end of line), and the column of
  end-of-line comments;
- **blank lines**, the file's **indentation unit** (2 spaces, 4, tab) and
  **the form of `rpc`s** (`;` or `{}`);
- **the `syntax` … `package` header** as written, including `option` lines
  and `import` order;
- **compact declarations** (`message A { int32 a = 1; }`).

**Robust to edits in Capella**: a comment's raw text is only reused as
long as the description is unchanged; an edited description is rendered
in the detected style. Likewise, the original header is only reused if
the imports and package required by the model are unchanged; otherwise it
is regenerated, and `option` lines are then lost.

**If absent**, export stays correct (valid file, identical semantics), but
with normalized formatting.


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
| Two **different** files define a type with the same name in the same folder | Flagged by an `ATTENTION` naming both files and the elements taken over: the last imported file takes the elements over, and the other file will no longer come out on export. Such files cannot coexist in a single proto build anyway. |
| Importing file B that was already pulled in transitively (via file A) | Finds the already-created elements in the right package, duplicates nothing — no matter which file you "enter" a proto module through, the end result converges. |

The script also flags, for information, any Classes in a package that
aren't related to any of the files processed in this particular
import (pre-existing unrelated content) — expected if your `DataPkg`
(or a sub-package) holds other elements too.


## 9. Known limitations

- **Enums nested inside a message**: not handled (Capella does not allow
  an `Enumeration` inside a `Class`); flagged with an `ATTENTION`, fields
  of that type are left untyped.
- **`reserved` and field options** (`[deprecated = true]`): not preserved.
  File-level `option` lines are only preserved while the original header
  can be reused (§7.8).
- **One Capella package = one folder**: two files in the same folder
  cannot define types with the same name, even in different proto
  packages; the import flags it (§8).