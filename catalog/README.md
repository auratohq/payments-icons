# Catalog format

`/catalog.json` is the versioned index. Each item in `categories` points to one JSON array at `/catalog/<category>.json`. `/catalog.csv` combines all records for spreadsheet use.

| Field | Meaning |
| --- | --- |
| `id` | Stable Aurato record ID |
| `name` | Display name from the source catalog |
| `aliases` | Alternative names that improve search and discovery |
| `category` | Root asset folder |
| `path` | Descriptive, SEO-friendly repository-relative PNG path |
| `upstreamPath` | Exact host-free 48-pixel WebP source path used by maintainers |
| `width`, `height` | Native PNG pixel dimensions |
| `bytes` | Exact file size |
| `sha256` | Lowercase SHA-256 of the complete file bytes |
| `source.kind` | Recorded provenance label; labels containing `generated` identify generated artwork |
| `source.url` | Recorded absolute HTTP(S) source URL, or `null` |
| `source.reference` | Optional older relative source reference, retained as text |

Names are not necessarily unique. Use `path` to identify a public file and `id` to connect it to the Aurato record. Several records may intentionally use identical image bytes, which is reflected in their shared hash. Counts describe records/files, not distinct logo designs.

All paths are one category plus one filename. Primary icons are 48 × 48; cards are 48 × 30. Every primary icon contains a rounded-square base in its alpha channel. The gallery does not add a CSS radius to disguise a different underlying silhouette.

Source WebPs are decoded to PNG. Cards with nonstandard canvases are trimmed to their visible artwork and fitted within 48 × 30 without stretching; native 48 × 30 cards keep their decoded pixels. Catalog hashes describe the resulting PNGs.

The index's `updatedAt` describes the snapshot date; `version` identifies the maintained collection version. Upstream hosts are deliberately excluded from public catalog files. Source URLs and origin labels describe provenance rather than licensing; see [RIGHTS.md](../RIGHTS.md).

Square icon sources receive the same baked-in rounded-square alpha frame as the rest of the pack during conversion.
