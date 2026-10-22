# Factory

Phase 4. Design: `docs/factory.md`. Nothing here runs in production; the factory only produces pull requests.

| Path | Purpose |
|---|---|
| `config.yaml` | pinned models, caps, labels |
| `bead_templates/` | one template per work type; the supervisor refuses beads that do not fill every section |
| `prompts/` | system prompts for workers and the PDF extractor; their SHA-256 is recorded on every run |
| `intents/` | human-written intent documents that the planner session turns into bead graphs |
| `supervisor/` | the loop that claims approved beads and runs workers |
| `sandbox/` | worker images and the egress allowlists |
