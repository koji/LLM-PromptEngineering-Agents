# Documentation Workflow

Category pages (`docs/categories/*/readme.md`) are **generated** from
`data/entries.yml` — do not edit them by hand. Schema reference:
`docs/data-schema.md`.

## Adding a resource

Add an entry to `data/entries.yml`:

```yaml
- name: Example Tool
  category: tools
  summary: One or two sentences that explain what it is and why it matters.
  url: https://example.com/tool
  tags: [tools]
```

Then run `python3 scripts/generate.py` and commit the YAML together with the
regenerated category pages.

## When to create an entry page

Create a page in `docs/entries/` when the resource needs:

- setup notes
- longer commentary
- comparisons
- screenshots
- maintenance notes

## Editing rules

- Keep entries easy to scan: one or two sentences for `summary`.
- Keep `summary` on a single line (the parser does not support `|` / `>` blocks).
- Prefer one clear category over duplicating the same resource across categories.
- Use the template in `docs/templates/entry-template.md` for new detail pages.
