# OAIMathDistill

An independent open source project that synthesizes training data from the public [OpenAI Math repository](https://github.com/openai/math) for supervised fine-tuning of open models' mathematical reasoning.

Distillation here means extracting training signal from released mathematical research through source-grounded synthesis and review. The [OpenAI mathematics release](https://openai.com/index/sharing-ai-progress-in-mathematics/) and [Mark Chen's post](https://x.com/markchen90/status/2107876922147668198) motivate this work. Whether the resulting data improves open models requires downstream evaluation.

## Capabilities

- Preserve native LaTeX sources with file hashes, catalogue status, result-family IDs, and revision metadata.
- Generate candidates through six configurable methods: local proof, proof completion, proof repair, reading, reflection, and exploration. Reading supports QA and connected explanations.
- Split native TeX into complete subsection units by default, retaining file/line provenance; accept supplied text, explicit line ranges, or an agent that selects passages.
- Use DeepSeek Flash with thinking enabled by default, or configure another chat-completions API or Codex CLI. Generation and review make separate requests.
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

Inspect the methods and preview the configured tasks without making model calls:

```sh
python3 -B -m oaimathdistill.generation list
python3 -B -m oaimathdistill.generation preview --summary
```

The [default configuration](configs/generation/default.toml) uses the official DeepSeek API, `deepseek-flash`, and explicit `thinking = {type = "enabled"}` with `reasoning_effort = "high"`. Put `DEEPSEEK_API_KEY=your-key` in a local project-root `.env`, or export that variable in your shell. The `.env` file is ignored by Git; credentials and authorization headers are excluded from run records. A nonempty environment variable takes precedence over `.env`.

The default selects the first **1,000 material units** in manuscript-ID order and then within-paper order, using the pinned [native manuscript manifest](configs/sources/native-manuscripts.json). It runs all six methods for each unit before proceeding to the next unit, allowing **at most one candidate per unit and method**: 6,000 construction jobs and up to 6,000 candidates. Empty results remain valid. With separate review and the two-stage repair method, this is at most 13,000 application calls before retries; concurrency remains one. Review uses the same settings in a fresh request.

Every new generation or review request includes a random 32-character hexadecimal `Random seed:` line. The seed is saved with the exact request, call trace, checkpoint, and candidate metadata. Completed steps keep their seeds on resume; an actual retry gets a new seed and retains earlier attempts. This prompt variation cue does not set the provider's numeric sampling seed. Inspect the preview and adjust the corpus offset/limit, methods, or backends before running:

```sh
python3 -B -m oaimathdistill.generation run --output runs/my-first-generation
python3 -B -m oaimathdistill.generation export runs/my-first-generation
```

Use `--config` to choose another TOML file. The [direct-text API example](configs/generation/chat-fragment.example.toml) uses the same DeepSeek thinking configuration for a supplied fragment. The [Codex example](configs/generation/codex-fragment.example.toml) uses an existing authenticated CLI installation.

The [package guide](oaimathdistill/README.md#mathematical-data-generation) documents inputs, method parameters, model settings, editable prompts, custom methods, review, trajectories, and resumption. The explicit [eight-task Codex example](configs/generation/pilot.toml) covers all six methods and both input modes with at most one candidate per task.

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
