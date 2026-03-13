# Future Proof Notes - Python

Python implementation track for the `future-proof` notes manager.

## Phase 1 Focus

Implement core note management features:
- Create, read, update, delete (CRUD) text notes
- Store notes as markdown files in `~/.notes/notes/`
- Support basic metadata (title, created/modified timestamps, tags) in YAML frontmatter
- Implement a simple CLI for managing notes
- Implement a basic search function that searches note content and metadata

But to get started, you'll need to read the python files here. they are Starter classes, and you can run them to see how they work. They are not complete, but they will give you a good starting point for your implementation. You can also refer to the Java implementation for guidance on how to structure your code and implement the required features.

```bash
# Show help
python3 notes0.py help

# No command (shows error)
python3 notes0.py

# Unknown command (shows error)
python3 notes0.py create

# Or as an executable:
./python/notes0.py help

```

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

## Python Technical Guidance

- Framework: Flask or FastAPI
- Validation/parsing:
	- `csv` (stdlib)
	- `json` (stdlib)
	- `PyYAML` for sidecar metadata
- Upload handling:
	- enforce allowed types (`.csv`, `.json`)
	- enforce max upload size
	- ensure UTF-8 decoding
- Profiling jobs:
	- run async profiling after upload (thread pool or task queue)
	- persist profile output back into sidecar metadata

## Integration Notes

- Keep a shared `Asset` model (`note` or `dataset`) behind service/repository interfaces.
- Store raw datasets unchanged; never rewrite uploaded source by default.
- Include datasets in backup/restore manifests.
- Add role checks for dataset operations (`viewer`, `editor`, `data-engineer`, `admin`).
