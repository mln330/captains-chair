from typing import Any

from make_it_so.baseline import BASELINE_BATCH_CHARS, baseline_batches


def test_baseline_batches_cover_documents_and_source_without_oversized_prompts() -> None:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": "now",
        "repo": "example/project",
        "default_branch": "main",
        "project_manifest": None,
        "github": {},
        "git": {},
        "documents": {"README.md": "D" * 80_000},
        "source_inventory": {"counts": {"source_files": 1}},
        "source_contents": {"src/app.py": "S" * 80_000},
        "dependency_manifests": {},
        "ci_workflows": {},
        "tests": [],
        "checks": [],
    }
    batches = baseline_batches(payload)
    combined = "".join(batches)
    assert len(batches) >= 3
    assert all(len(batch) <= BASELINE_BATCH_CHARS for batch in batches)
    assert "D" * 1000 in combined
    assert "S" * 1000 in combined
    document_chunks = [batch for batch in batches if "D" * 1000 in batch]
    source_chunks = [batch for batch in batches if "S" * 1000 in batch]
    assert document_chunks and all("## Canonical document: README.md" in batch for batch in document_chunks)
    assert source_chunks and all("## Source file: src/app.py" in batch for batch in source_chunks)
