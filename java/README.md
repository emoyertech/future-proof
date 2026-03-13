# Future Proof Notes - Java

Java implementation track for the `future-proof` notes manager.

## Phase 1 Focus

Implement core note management features:
- Create, read, update, delete (CRUD) text notes
- Store notes as markdown files in `~/.notes/notes/`
- Support basic metadata (title, created/modified timestamps, tags) in YAML frontmatter
- Implement a simple CLI for managing notes
- Implement a basic search function that searches note content and metadata

But to get started, you'll need to read the java classes here. they are Starter classes, and you can run them to see how they work. They are not complete, but they will give you a good starting point for your implementation. You can also refer to the Python implementation for guidance on how to structure your code and implement the required features.

```bash
# Compile (already done)
cd java
javac NotesShell.java Notes0.java Notes1.java

# Run interactive shell
java NotesShell

# Run CLI versions
java Notes0 help
java Notes1 help
java Notes1 list

# Setup test notes
mkdir -p ~/.notes/notes
cp test-notes/*.md ~/.notes/notes/
java Notes1 list
```

These commands will help you get familiar with the codebase and see how the note management features are expected to work. You can then start implementing the missing features and improving the existing ones based on the requirements outlined in this README.

## Phase 2 Focus

Add REST + web support for both:
- text notes
- dataset files (`.csv`, `.json`) for Data Engineer workflows

## Dataset Support (CSV/JSON)

Use filesystem-first storage with sidecar YAML metadata.

Example layout:

```
~/.notes/
	notes/
		2026-03-13-my-note.note
	datasets/
		sales-2026-q1.csv
		sales-2026-q1.dataset.yml
		customer-events.json
		customer-events.dataset.yml
```

Dataset sidecar fields (minimum):
- `id`, `title`, `author`, `created`, `modified`, `tags`
- `format` (`csv` or `json`)
- `path` (relative to `datasets/`)
- `rowCount`
- `schema` (list of `{name, type}`)

Canonical spec example:
- [docs/dataset-metadata-schema.example.yml](../docs/dataset-metadata-schema.example.yml)

## Phase 2 API Endpoints

```
GET    /api/notes
POST   /api/notes
GET    /api/notes/:id
PUT    /api/notes/:id
DELETE /api/notes/:id

GET    /api/datasets
POST   /api/datasets             # Upload CSV/JSON
GET    /api/datasets/:id
DELETE /api/datasets/:id
GET    /api/datasets/:id/preview # First N rows
GET    /api/datasets/:id/profile # Column stats and inferred types

GET    /api/search?q=query       # Search notes + datasets
```

## Java Technical Guidance

- Framework: Spring Boot (recommended)
- Validation/parsing:
	- CSV: Apache Commons CSV (or Jackson CSV)
	- JSON: Jackson
	- YAML sidecar: Jackson dataformat YAML or SnakeYAML
- Upload handling:
	- multipart upload endpoint
	- allow only `.csv` and `.json`
	- enforce max upload size via config
	- enforce UTF-8 decode checks
- Profiling jobs:
	- async profiling after upload (`@Async`/executor)
	- persist profile output back into sidecar metadata

## Integration Notes

- Keep a shared `Asset` model (`note` or `dataset`) behind service/repository interfaces.
- Store raw datasets unchanged; never rewrite uploaded source by default.
- Include datasets in backup/restore manifests.
- Add role checks for dataset operations (`viewer`, `editor`, `data-engineer`, `admin`).
