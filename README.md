# .proto <-> Capella Import / Export

*[Version française](README.md)*

Command-line tools to import gRPC services (`.proto` files) into a
Capella model (Interfaces, Classes, Services, Parameters), and to
export Capella Interfaces back to `.proto` files.

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
| `proto_capella_types.py` | Shared mapping between proto types and Capella `DataType`, plus the PVMT keys for streaming. **Adjust once** to match the real names used in your project (see §4). |
| `import_proto_to_capella.py` | Imports a `.proto` file into a Capella model (Classes, Interfaces, Services, Parameters, comments, streaming). |
| `export_capella_to_proto.py` | Exports a Capella Interface to a `.proto` file. |
| `verify_import.py` | Re-reads a model after import and prints what was created, for a quick sanity check. |
| `list_datatypes.py` | Lists the `DataType`s existing in a layer's `DataPkg`, to help fill in `proto_capella_types.py`. |
| `route_guide.proto` | Sample `.proto` file (official gRPC example) for testing: covers all 4 RPC modes (unary, client/server/bidirectional streaming) and nested messages. |

Keep all `.py` files in the same folder: the scripts import each
other (`from proto_capella_types import ...`).


## 3. Usage

### Import: `.proto` -> Capella

```bash
python import_proto_to_capella.py my_service.proto My_Model.aird [--layer=la]
```

- `--layer`: target layer, `oa` / `sa` / `la` (default) / `pa` —
  Operational Analysis, System Analysis, Logical Architecture, Physical
  Architecture respectively. For gRPC software interfaces, `la` or `pa`
  are the relevant choices (not `oa`, which is for operational/business
  needs).
- The script creates: one `Class` per message, one `Property` per
  field, one `Interface` per service, one `Service` (operation) per
  `rpc`, with its typed input/output `Parameter`s.
- Comments in the `.proto` file (above each message/field/service/rpc)
  are carried over into the corresponding Capella `description`.
- The streaming mode (`stream` on the request and/or the response) is
  stored via PVMT (see §5) on the operation.
- **Model prerequisites**, in the target layer: an existing `DataPkg`
  and `InterfacePkg` (created by default by Capella), and the PVMT
  domain/group for streaming (§5).

After importing: close and reopen the project in Capella (or
*Refresh*) to see the changes — the script modifies the file on disk
without going through any Capella instance that might be open.

To check what was created without going back into the Capella UI:

```bash
python verify_import.py My_Model.aird ServiceName
```

### Export: Capella -> `.proto`

```bash
python export_capella_to_proto.py My_Model.aird ServiceName output.proto [--layer=la]
```

- `--layer`: optional, only needed to disambiguate if an Interface
  with the same name exists in several layers (the script stops with
  an explicit error in that case if `--layer` isn't given).
- Regenerates `syntax = "proto3";`, a `message` for each data type
  referenced by the service, then the `service` with its `rpc`
  entries (including `stream` recomputed from PVMT), and comments
  taken from the Capella `description` fields.


## 4. Configuring primitive types (once per project)

`proto_capella_types.py` holds the mapping between `.proto` primitive
types (`string`, `int32`, `bool`...) and Capella `DataType` names
(`String`, `Integer`, `Boolean`...). These names are project-specific
— check yours with:

```bash
python list_datatypes.py My_Model.aird
```

Then adjust the values in `PROTO_TO_CAPELLA_PRIMITIVE` (inside
`proto_capella_types.py`) to match the real names. This file is
shared by both import and export: a single edit covers both
directions.

Until this is done, affected fields are created/exported **without a
resolved type** (a warning is printed at runtime, the script does not
stop).


## 5. PVMT procedure (gRPC streaming) — once per project

The streaming mode (`stream` on the request and/or the response) is
stored as a Property Value via Capella's PVMT extension. This
domain/group must be created **manually in Capella**: neither
Python4Capella nor `capellambse` support creating it via script (a
deliberate limitation of both tools).

**Without this step**, the scripts still run, but the import script
**stops before making any change**, with an explicit message, as long
as the structure doesn't exist yet (automatic pre-flight check).

### Steps

1. Make sure the **PVMT** add-on is installed in Capella
   (`Help > Install New Software`).
2. Select any model element, open the **Property Values** view
   (`Window > Show View > Other... > Property Values`).
3. From that view, open the **PV Definition Editor**.
4. Create the following structure:

   | Level | Name | Type |
   |---|---|---|
   | Domain | `Grpc` | — |
   | Group (inside the Domain) | `Streaming` | — |
   | Property (inside the Group) | `ClientStreaming` | Boolean |
   | Property (inside the Group) | `ServerStreaming` | Boolean |

5. Set the Group's **Scope** to the layer(s) where your interfaces
   live (typically Logical Architecture and/or Physical Architecture
   — **not** "Operational", which refers to the Operational Analysis
   layer and has nothing to do with interface operations).

Reference video (official Thales demo, domain creation starting at
12:39): [Easily enrich Capella models with your domain extensions](https://www.youtube.com/watch?v=ieVmw54YE94)

### Known caveat

A known, still-open Capella bug (GitHub issue
eclipse-capella/capella#2719) crashes the *Property Values* view when
clicking directly on a boolean value to edit it manually in that
view. This only affects manual editing: our scripts set values through
the `capellambse` API, not through that view, and are unaffected.

The names `Grpc`, `Streaming`, `ClientStreaming`, `ServerStreaming`
are the defaults used in `proto_capella_types.py`
(`PVMT_CLIENT_STREAMING_KEY` / `PVMT_SERVER_STREAMING_KEY`). If you
use different names, update those two constants accordingly.


## 6. Known limitations

- **`repeated` fidelity**: export determines `repeated` from the
  Capella `Property`'s cardinality (`max_card`). The current import
  script does not set this cardinality explicitly, so a `repeated`
  field that's imported and re-exported will come back as a plain
  singular field. Fix on the import side if a fully faithful
  round-trip on lists is needed.
- **Primitive types and PVMT**: both depend on per-project
  configuration (§4 and §5), done once.
- **`oneof`, `map<>`**: not handled by the current scripts.