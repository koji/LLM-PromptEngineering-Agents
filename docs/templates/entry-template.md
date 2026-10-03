# Resource Name

New resources are added to `data/entries.yml` (see `docs/data-schema.md`).
Use this snippet:

```yaml
- name: Resource Name
  category: tools            # one of the slugs in data/entries.yml
  summary: One or two sentences that explain what the resource is and why it matters.
  url: https://example.com
  tags: [tools]
```

For table-layout categories, also set `section:` (and `subsection:` if any).
Then run `python3 scripts/generate.py`.

---

The template below is for detailed pages under `docs/entries/`,
when a resource needs more than a short entry.

## Summary

One or two sentences that explain what the resource is and why it matters.

## Links

- Homepage:
- Repository:
- Documentation:

## Tags

- agent
- prompt-engineering
- library

## Notes

- Key capability:
- Best use case:
- Caveat:
