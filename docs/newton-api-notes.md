# Newton API notes: image inputs to `/query`

Backing notes for `newton_analyze_image` (issue #3). Every field the implementation
sends is traceable to a public doc page below. Not validated against a live Archetype
account — see the README status line.

## Documented / source table

| Field / thing | Documented? | Source |
|---|---|---|
| `events` accepts `data.base64_img`; payload shapes per Data Events | yes | `/api-reference/query` -> links `/core-concepts/streams/events/data-events` |
| `data.base64_img` -> `event_data.contents` (str, required, "the base64 encoded image as a byte string"), with example | yes | Data Events page (marked archived; the current `/llms.txt` states its payload shapes are the ones Direct Query uses in `events`) |
| `file_ids`: pass the extension-bearing `file_id`, not `file_uid`; `/query` filters by extension; `.png`/`.jpg`/`.jpeg` contents are injected into the Newton text model's context | yes | `/api-reference/query` |
| `POST /v0.5/files`, `multipart/form-data`, `-F "file=@..."`; response `is_valid`, `file_id`, `file_uid`; 512 MB max; JPEG/PNG among accepted types | yes | `/api-reference/files/upload` |
| `POST /v0.5/files/base64`, multipart form field `file` holding base64 text | yes | `/api-reference/files/upload-base64` |
| `mime_type` as any API field | no | gateway-side validation only (schema enum image/png, image/jpeg, or null); never sent on the wire. Deliberate input rejections surface as actionable tool errors |
| `data.json` `event_data` shape (arbitrary keys, no `contents` wrapper) | documented, but differs from what `newton_query` currently sends | Data Events page. `newton_query` currently wraps JSON as `{"contents": ...}`; **not changed by this proposal**, tracked as an open item for issue #12 |

## Documented but unused by this change

- `file_ids` also accepts `.txt`, `.json`, `.csv` and `.mp4` per `/api-reference/query` —
  `newton_analyze_image` only accepts image extensions (`.png`, `.jpg`, `.jpeg`), since it
  is scoped to image questions.
- `data.base64_img_array`, `max_frames`, batching/multi-image and video inputs are
  documented elsewhere but out of scope for this tool (see the proposal's "Out of scope").

## Limits

- `POST /v0.5/files` accepts up to 512 MB (`DOCUMENTED_MAX_UPLOAD_BYTES` in
  `config.py`), per the Files API upload page.
- `newton_analyze_image`'s inline path additionally enforces `NEWTON_MAX_IMAGE_BYTES`
  (default 8 MiB) on the *decoded* byte count before any network call. This limit is
  transport-shaped (the base64 payload arrives inside a JSON MCP tool argument) and is
  not itself a documented Archetype limit — it exists to keep an oversized inline payload
  from ever reaching the wire.

## Multipart part name for `POST /v0.5/files`

The `/api-reference/files/upload` page's example uses a cURL invocation of the form
`-F "file=@..."`, i.e. the multipart part name is `file`. This repo's
`ArchetypeNewtonBackend.upload_image` sends the same part name. This has not been
verified against a live account; if a live smoke test finds the API expects a different
part name, update this note and the adapter together.

## Not validated against a live account

Everything in this file and the `newton_analyze_image` tool it backs is implemented
against public documentation only. No call in this repository has been exercised
against a real, authorized `ATAI_API_KEY`. Treat the mock backend's output as a wiring
check, never as a preview of real inference — see issue #12 for live validation.
