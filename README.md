# OAIMathDistill

An independent open source project that synthesizes training data from the public [OpenAI Math repository](https://github.com/openai/math) for supervised fine-tuning of open models' mathematical reasoning.

Distillation here means extracting training signal from released mathematical research through source-grounded synthesis and review. The [OpenAI mathematics release](https://openai.com/index/sharing-ai-progress-in-mathematics/) and [Mark Chen's post](https://x.com/markchen90/status/2107876922147668198) motivate this work. Whether the resulting data improves open models requires downstream evaluation.

## Capabilities

- Preserve native LaTeX sources with file hashes, catalogue status, result-family IDs, and revision metadata.
- Generate candidates through six configurable methods: local proof, proof completion, proof repair, reading, reflection, and exploration. Reading supports QA and connected explanations.
- Accept supplied text, native files with selected line ranges, or an agent that selects passages from a manuscript.
- Use Codex CLI or configurable chat-completions APIs, with separate generation and review requests.
- Retain local call trajectories, intermediate results, checkpoints, and review evidence; export reviewed examples as `messages` with provenance and check metadata.
- Extend methods and prompts without changing the shared runner. Every method may return no candidates when the source is unsuitable.

The active input selection is the 719 current-catalogue research manuscripts with native LaTeX. PDF processing is deferred. Lean sources remain available upstream; a formal-proof generation and checking pipeline is not implemented. Model review and structural checks do not provide an independent mathematical proof certificate. Training and downstream evaluation are future work.

## Get the sources

The upstream collection is a Git submodule pinned to [`1581d4cbdd69a0faf34b2089614fe2a814316a6a`](https://github.com/openai/math/tree/1581d4cbdd69a0faf34b2089614fe2a814316a6a). Its catalogue contains 719 manuscripts in 372 result families.

```sh
git clone --recurse-submodules https://github.com/bebetterest/OAIMathDistill.git
cd OAIMathDistill
```

For an existing checkout:

```sh
git submodule update --init math
```

## Quick start

Run from the project root with Python 3.11 or later. The Python code uses only the standard library; running it from the checkout requires no package installation.

Inspect the methods and preview one task without making model calls:

```sh
python3 -B -m oaimathdistill.generation list
python3 -B -m oaimathdistill.generation preview --config configs/generation/codex-fragment.example.toml --summary
```

The Codex example uses an existing authenticated CLI installation. Edit [the API example](configs/generation/chat-fragment.example.toml) to use a chat-completions endpoint instead. After adjusting the selected configuration:

```sh
python3 -B -m oaimathdistill.generation run --config configs/generation/codex-fragment.example.toml --output runs/my-first-generation
python3 -B -m oaimathdistill.generation export runs/my-first-generation
```

The [package guide](oaimathdistill/README.md#mathematical-data-generation) documents inputs, method parameters, model settings, editable prompts, custom methods, review, trajectories, and resumption. The [eight-task example](configs/generation/pilot.toml) covers all six methods and both input modes with at most one candidate per task.

To preserve available native source files as separate text bundles:

```sh
python3 -B -m oaimathdistill.extract_sources \
  --source-root math \
  --output-dir data/extracted/native-text \
  --extract-text
```

Use a fresh output directory. Omit `--extract-text` for a source inventory without exporting file contents. PDF paths are retained as catalogue metadata; PDF files are not read or converted.

Run the offline software checks:

```sh
python3 -B -m unittest discover -s tests -v
```

## Repository layout

| Path | Purpose |
| --- | --- |
| `math/` | Original research collection at the pinned upstream revision. |
| `oaimathdistill/` | Native-source extraction, generation, review, and export code, with its usage guide. |
| `configs/generation/` | Editable Codex, API, and multi-method examples. |
| `tests/` | Offline regression and integration checks using fixtures. |

Generated datasets, execution logs, trajectories, process notes, and local backups are maintained outside version control. The repository contains code, reusable configurations, and documentation of current behavior.

## License and attribution

Project code and original documentation are licensed under [Apache-2.0](LICENSE). The upstream collection retains its own license and attribution. Dataset releases must document their source licenses and transformations.

This is an independent project. It is not affiliated with OpenAI.
