# nli-span-labeler - Inventory Report (2026-04-05)

## Summary
A FastAPI web application for human annotation of Natural Language Inference (NLI) examples at the word-span level. Annotators select individual words in premise/hypothesis pairs and assign them labels covering difficulty dimensions (reasoning, creativity, domain knowledge, etc.), NLI relations (entailment, neutral, contradiction), and custom labels. The tool supports multi-annotator workflows with authentication, locking, progress tracking, and JSONL export. A tiered annotation system (Tier 0 automatic labels via rule-based heuristics, Tier 1 WordNet-based semantic relations) reduces manual burden. Built as a GoblinCorps team project with a defined PR/review workflow.

## Technologies & Libraries
- **Language**: Python
- **Web Framework**: FastAPI + Uvicorn + Starlette
- **Database**: SQLite (`labels.db`, ~299KB of annotation data present)
- **Tokenization**: HuggingFace Transformers (ModernBERT tokenizer as default: `answerdotai/ModernBERT-base`)
- **Datasets**: HuggingFace Datasets (SNLI, MNLI, ANLI)
- **Weak Supervision**: NLTK (WordNet semantic relations, Tier 1 labels)
- **Testing**: pytest, httpx (FastAPI TestClient)
- **Frontend**: Single-page app (vanilla JS in `static/index.html`)
- **Dependency management**: uv (pinned freeze at Mar 2026)
- **CI**: GitHub Actions (`.github/` present)

## Timeline of Work
- **Dec 8, 2025**: Project initialized — core app, data download scripts, run.sh, .gitignore
- **Dec 8–13, 2025**: Rapid development sprint — API fixes, frontend consolidation, test rewrites, auth system, anonymous mode, tiered annotation system (Tier 0 automatic labels), WordNet Tier 1 labels
- **Dec 13, 2025**: Merge PR #2 (GoblinCorps/master → main); final feature: conditional difficulty span justifications and complexity slider rescaled 0–10
- **Mar 12, 2026**: Environment maintenance (venv rebuild, uv freeze update)
- **42 commits** recorded since Jan 1, 2025 — all within the Dec 2025 sprint

## Current State
- **Functional**: Yes — `labels.db` contains actual annotation data (299KB), indicating real use
- **Uncommitted changes**: None apparent — last substantive commit was Dec 13, 2025
- **Running**: `./run.sh` starts uvicorn on port 8000; data must be downloaded via `scripts/download_data.py`
- **Test suite**: Present in `tests/`, pytest.ini configured; `.pytest_cache/` indicates tests have been run
- **Last work**: Tiered annotation system with WordNet semantic relations + complexity slider UI changes

## Successes
- Fully functional annotation tool with authentication, multi-user locking, and progress stats
- Tiered weak supervision reduces annotation load: Tier 0 (automatic rule-based) and Tier 1 (WordNet relations) pre-fill labels
- Clean REST API with well-documented endpoints
- Actual annotation data in `labels.db` — the tool was actively used, not just built
- Well-organized team workflow (GoblinCorps CLAUDE.md with PR/review process)
- MIT license, ready for public release

## Open Items / Failures
- README still has placeholder `YOUR_USERNAME` in clone URL — never finalized for public release
- NLTK dependency listed in requirements.txt but absent from uv freeze — potential install inconsistency
- No inter-annotator agreement metrics visible in feature list (though agreement endpoints exist in the API)
- No deployment configuration (Docker, systemd, etc.) — local-only usage
- Single monolithic `app.py` at 208KB — likely needs refactoring for maintainability at scale

## Long-term Vision
The tool appears designed to produce high-quality, span-annotated NLI training data with difficulty metadata. The tiered labeling system suggests an intent to generate weakly supervised training data at scale, with human annotations layered on top for quality. The difficulty dimensions (reasoning, creativity, domain_knowledge, etc.) point toward research into what makes NLI examples hard — potentially to train or evaluate models on difficulty-stratified subsets or to generate harder training examples.

## Portfolio Fit
Demonstrates full-stack web development skills (FastAPI backend, vanilla JS frontend, SQLite), NLP domain expertise (NLI, tokenization, WordNet), and collaborative software engineering (team CLAUDE.md, PR workflow, CI). The tiered weak supervision system shows ML research sophistication. The presence of real annotation data suggests practical deployment experience, not just a demo. Good evidence of building research tooling that bridges NLP theory and usable software.
