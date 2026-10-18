# Processing and synthesis code

This directory contains the project's Python package. It inventories native LaTeX and constructs mathematical training candidates through configurable methods.

## Source extraction

Run from the project root with Python 3.11 or later. The extractor uses only the standard library:

```sh
python3 -B -m oaimathdistill.extract_sources \
  --source-root math \
  --output-dir data/extracted/native-text \
  --extract-text
```

Use a fresh output directory for each run. Omit `--extract-text` for a source inventory without file contents. Native `.tex`, `.bib`, `.sty`, and `.cls` sources are exported as JSON bundles with per-file text, byte hashes, and source revision. TeX files are not merged or executed, and non-text assets are not copied.

The output contains `inventory.jsonl`, `coverage.json`, and `<kind>/<name>/latex_sources.json` for artifacts with native source. Coverage format version 2 records `source_bundles_written`, `source_files_preserved`, encoding issues, and `skipped_no_native_source`. Skipped records have no output directory. Original PDF paths remain inventory references; PDFs are not read, converted, or copied.

Source availability does not establish mathematical correctness or training value. Withdrawn and unlisted artifacts retain their original status and require a separate inclusion decision.

## Mathematical data generation

Python 3.11+ and the standard library are sufficient. The Codex backend uses an existing CLI installation and authentication; the chat backend sends HTTP JSON requests without an SDK. No separate environment or model download is required.

The default is [complete TeX subsection units with DeepSeek Flash thinking](../configs/generation/default.toml). Put `DEEPSEEK_API_KEY=your-key` in the ignored project-root `.env`, or export that variable. The configured `env_file = ".env"` is a credential fallback; a nonempty environment variable wins. Generation and review use independent requests with `model = "deepseek-flash"`, `thinking = {type = "enabled"}`, and `reasoning_effort = "high"`. The API's exposed reasoning is retained in each raw response, separately from the student training messages.

The default selects the first 1,000 corpus units, applies six methods to each unit with at most one candidate per method, uses concurrency one, and sets a 600-second socket timeout per call. This gives 6,000 construction jobs and up to 6,000 candidates; empty results are normal. Independent review plus the extra draft stage for repair gives at most 13,000 application calls before retries. Jobs run by source, completing the six configured methods for one unit before starting the next. `max_tokens = 32768` bounds the provider's combined thinking/final output. Truncation fails the call and preserves the raw response; raise the limit in a fresh run when needed. No temperature override is supplied because DeepSeek thinking mode ignores it. See the official [thinking guide](https://api-docs.deepseek.com/guides/thinking_mode/) and [Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/) for provider parameters.

The [API fragment example](../configs/generation/chat-fragment.example.toml) uses the same settings for literal text. The [Codex fragment example](../configs/generation/codex-fragment.example.toml) remains available. Edit the selected configuration before calling a model. If you copy a file elsewhere, update `project_root`, which is relative to the configuration file; all other configured paths are relative to that project root.

```sh
python3 -B -m oaimathdistill.generation list
python3 -B -m oaimathdistill.generation preview --summary
python3 -B -m oaimathdistill.generation run --output runs/my-first-generation
python3 -B -m oaimathdistill.generation export runs/my-first-generation
```

`preview` and `run` use `configs/generation/default.toml` when `--config` is omitted. `preview --summary` shows corpus selection and method counts, then the first 20 resolved jobs and their character counts, input/method/form, generation and review backends, model and thinking settings, timeouts, concurrency, candidate limit, output-directory state, and a bound on application calls before retries for the built-in methods. Zero candidates can reduce the actual calls. The bound excludes retries and any provider/CLI reconnects; custom methods determine their own calls. An existing output with a different fingerprint is marked `fingerprint_mismatch` and requires a fresh `--output`.

Summary mode omits prompt rendering and caps displayed job rows at 20; all selected jobs are retained. Omit `--summary` to inspect the complete first prompt and output schema of every job as JSON. Redirect a large full preview to a local file if needed. Seed lines use a fixed placeholder during preview; preview never draws random seeds. Preview resolves inputs and validates configuration without making model calls or writing run files. Multi-step methods identify later stages. An external method without `preview` shows a notice instead of executing `generate`.

### Where to adjust behavior

| Change | Configuration or code |
| --- | --- |
| Input passage | `[[sources]]`: literal `text`, or a `path`. TeX defaults to complete subsection units. Use `units = [1, 3]` to select them, or explicit `ranges` for a manual fragment. |
| Corpus window | `[[sources]]`: `manifest`, zero-based `offset`, and positive `limit`. The default uses offset 0 and limit 1000 before method expansion. |
| Unit granularity/size | `unit = "subsection"` (default), `"section"`, or `"file"`; optional `max_chars` rejects oversized inputs without truncating. |
| Agent-selected passage | `[[sources]]`: `mode = "agent"` and a native manuscript `path`; requires Codex for generation and review. |
| Method selection | `[[methods]]`: `name`, `sources`, optional distinct `id`, and `enabled = false` to disable an entry. |
| Number and reading form | `params.count` is an upper bound; `params.form` is `qa` or `explanation` for reading. |
| Topic, emphasis, or other instructions | Additional `params` are serialized into the method prompt, for example `focus = "assumptions"`. |
| Model/API | Named `[backends.NAME]` tables, then `[generation].backend` and `[review].backend`. |
| Sampling/provider options | Backend `temperature`, `max_tokens`, `token_field`, or API `extra`; Codex uses its documented `overrides`. |
| Prompt wording or review rubric | `[prompts]` maps prompt names to editable English files; templates are in `generation/prompts/`. |
| Prompt variation | `[run].prompt_seed = true` adds and records one fresh random string per real call; set false to disable. |
| Job sequence | `[run].job_order = "source"` groups methods by material unit; `"method"` processes sources by method. |
| Concurrency and timeout | `[run].concurrency` limits active jobs; each backend has `timeout` in seconds. |
| Output and resumption | `[run].output` or CLI `--output`; rerun an identical configuration to reuse checkpoints. |
| A new construction algorithm | Add a Python method file and register it with top-level `method_files`. |

Fixed configuration fields reject unknown keys and invalid types before running, including backend-specific settings. `params` and API `extra` remain extensible. Extra method parameters influence the prompt; they do not create new runtime behavior unless the method implements it. Put model sampling settings under the backend rather than method parameters. Configuration values must be JSON-compatible and finite; TOML date/time literals are not supported.

The explicit [eight-task Codex configuration](../configs/generation/pilot.toml) uses manual line ranges and agent input; it overrides the default backend and slicing settings. It covers all six methods and both input modes using one native manuscript, with at most one candidate per task. Preview it with `--summary`, then select a fresh output directory before running.

`run.concurrency` limits active jobs. Built-in methods make sequential calls inside each job; a custom method that introduces its own parallelism controls those additional calls. Codex needs its normal launcher access to its login/runtime state even though model-generated commands run in the read-only sandbox; an enclosing OS sandbox can block CLI initialization.

### Methods and absence

| Method | Target |
| --- | --- |
| `local_proof` | A source-supported local proof with explicit premises. |
| `proof_completion` | A meaningful consecutive missing inference. |
| `proof_repair` | Diagnosis and repair of a documented flaw; draft and repair calls checkpoint separately. |
| `reading` | Comprehension QA (`form = "qa"`, default) or a connected explanation (`form = "explanation"`). |
| `reflection` | Grounded analysis of strategy, assumptions, or logical boundaries. |
| `exploration` | Motivated questions, connections, or conjectures with explicit uncertainty. |

`count` is a positive upper bound. Every method may return `{"candidates": [], "empty_reason": "specific reason"}`. Empty results are successful outcomes; the runner skips repair/review stages that have no candidates and does not generate replacements to meet a quota. Controlled variation is deferred.

### Inputs and configuration

TOML paths are resolved against `project_root`, which is relative to the configuration file. `source_root` defaults to `math`. A source has a unique `id` and a `mode`:

- `fragment`: provide `text`, or a native text `path`. A `.tex` path without explicit ranges defaults to `unit = "subsection"` and creates one source per complete unit. Non-TeX text files remain whole. Use `unit = "section"` for larger units or `unit = "file"` for the original complete file, including its preamble. Explicit `ranges = [[start, end], ...]` bypass automatic slicing; they are 1-based and inclusive, and noncontiguous ranges stay together as one identified fragment. A fragment can be a definition, example, discussion, or incomplete argument.
- `agent`: provide a file or manuscript-directory `path`. Codex receives the selected native file list and reads local material itself. Both generation and review need a file-capable backend for this input.

Paths inside the upstream tree must belong to a current-catalogue manuscript. PDFs, withdrawn manuscripts, summaries, and unlisted papers are outside this selection. Direct text and external local text files are accepted as user-supplied fragments with hashes and no invented attribution. Source files are read without executing TeX or expanding macros. Automatic heading units follow literal static includes while preserving original source files and line ranges.

Automatic units use literal `\subsection` headings; nested subsubsections remain in their parent unit. Authors sometimes divide a long proof into subsections. Those boundaries are preserved and `heading_environments` records any literal environments surrounding the selected heading. Such a unit may depend on earlier proof assumptions; select a larger unit or explicit context when necessary. A section without subsections remains a complete section. A document with no recognized headings falls back to its body. Parent introductory text is attached to its first child unit; other units do not automatically repeat it. Preambles and explicit `thebibliography` environments are excluded from automatic body selection. Comments, verbatim examples, escaped backslashes, and braced macro bodies do not create headings. Starred headings, optional short titles, and nested braces in titles are supported.

The reader resolves literal `\input`, `\include`, `\subfile`, `\import`, and `\subimport` paths. Upstream includes must remain within the selected manuscript's tracked native sources; external TeX inputs may include `.tex` files inside the entry file's directory tree. Missing, ambiguous, dynamic, escaping, and cyclic includes fail explicitly. Select the actual native body file when an entry point is a preamble or PDF assembly wrapper. `includeonly` and PDF assembly wrappers fail explicitly. This static reader does not evaluate macros, conditionals, or package-defined headings, and does not retrieve definitions or references from other units. Units are material for generation; their titles do not certify self-contained mathematics. Source blocks retain complete original lines, so a boundary line containing adjacent TeX can appear in both neighboring units.

Use `units = [1, 3]` to select distinct 1-based unit numbers in document order. Omit it to use all resolved units from that source. Generated IDs are `<source-id>::0001`, and so on; selection metadata records the requested/actual unit kinds, title hierarchy, original index, available unit count, and input character count. Method `sources = ["paper"]` expands to every selected unit in that group; `sources = ["paper::0003"]` selects one resolved unit. Preview shows the expansion before any calls. Each unit-method pair is a separate job, so adding units increases the call budget.

For example, with an actual native TeX path:

```toml
[[sources]]
id = "paper"
mode = "fragment"
path = "math/preprints/PAPER/build/main.tex"
unit = "subsection"
units = [2]
# max_chars = 40000
```

No automatic character limit is imposed and units are never silently clipped. Optional `max_chars` rejects selected inputs above a raw-character limit; it is not a tokenizer estimate. Increase the limit, choose other units, or provide explicit ranges after inspection. `unit`, `units`, and `max_chars` are rejected with literal text, agent mode, or explicit ranges. `units` additionally requires automatic TeX slicing.

Ranges must be a nonempty list of valid 1-based inclusive pairs, and their selected text must contain content. `ranges` is rejected for literal `text` and `agent` inputs, where it would otherwise be ignored. File hashes are computed from the same bytes used to build each fragment.

For a corpus window, use a pinned native-source manifest instead of one file:

```toml
[[sources]]
id = "papers"
manifest = "configs/sources/native-manuscripts.json"
unit = "subsection"
offset = 0
limit = 1000
```

The reusable manifest lists native body entry points for the pinned upstream revision. It contains 718 manuscript paths and an explicit exclusion with the missing-include reason for one additional manuscript. It contains source selection metadata, not execution logs. The loader checks the upstream revision, sorts manuscript IDs, follows document unit order, and applies the zero-based offset and unit limit before multiplying by methods. Therefore `limit = 1000` means 1,000 material units, rather than 1,000 method jobs. A smaller test can use `limit = 1`; a later batch can use `offset = 1000` and a fresh output directory.

Metadata records a 1-based `corpus_unit_index`, manifest path/hash/revision, paper and local unit information. Manifest files are fingerprinted and rechecked during execution. Invalid or missing inputs fail explicitly; only declared exclusions are omitted. If a requested window is too large, preview reports insufficient units instead of silently shortening the batch. Automatic main-file discovery is not performed: update the curated entry points when changing the upstream snapshot. `max_chars` checks selected units; it never clips them.

`[[methods]]` entries select a `name`, optional instance `id`, `sources`, and `params`. Omit `sources` to use every configured input. The same method can appear more than once with different forms or parameters; `enabled = false` disables an entry. For example:

```toml
[[methods]]
name = "reading"
id = "explain_selected_passages"
sources = ["my_fragment"]
params = {count = 2, form = "explanation"}
```

`[generation].backend` selects a named `[backends.NAME]`. `[review].backend` defaults to the generation backend; review always makes a fresh request/session. Codex inherits the configured model/profile by default and records that inheritance and the CLI version; an unreported resolved model remains unknown. Set `model` or `profile` to override it. Optional Codex `overrides` supports `model_reasoning_effort` and `model_verbosity`. A child CLI clears the inherited internal desktop-originator variable before starting and records the removed variable's name; the parent environment and global configuration remain unchanged.

For HTTP models, copy [the chat fragment example](../configs/generation/chat-fragment.example.toml). Set `kind = "chat"`, `base_url`, and `model`. Use `api_key_env` for the name of a credential variable, never its value. A base URL can be an API root or the full chat-completions endpoint. `max_tokens`, `token_field`, `temperature`, `timeout`, `json_mode`, and provider-specific `extra` fields are configurable. Optional `env_file` names a credential fallback file relative to `project_root` and requires `api_key_env`. Only that variable is read; ordinary unquoted/single-quoted/double-quoted values, optional `export`, and comments are supported. Shell commands and variable interpolation are not executed. Duplicate/malformed assignments fail without printing their values. The file's contents are excluded from fingerprints and run artifacts; changing a credential does not change a dataset's identity. Reserved routing/token fields cannot be overwritten through `extra`. Loopback servers bypass system HTTP proxies. Truncation, refusal, tool-call responses, and invalid JSON are failures rather than training examples.

Configured backends are validated during preview, but only the selected generation/review backends are started during a run. Keeping an unused Codex entry in an API experiment does not require a working Codex executable. Version probing is optional metadata; an unavailable probe is recorded as `unavailable`, while actual call failures keep their own error records.

Fragment input supports API generation and API review, including literal `text`, a text-file `path`, and selected line ranges. Python reads the selected text into each request; the model does not need filesystem tools. Generation and review may use different endpoints/models, the same API configuration with separate requests, or a mixture of API and Codex backends. The DeepSeek examples reuse the named `deepseek` backend in separate requests. Add another backend table and change `[review].backend` to use a different model or provider for review. Backend choice does not restrict the six construction methods.

### Editable prompts and custom methods

Default English prompts live in `generation/prompts/`. Override them in a configuration without changing package code:

```toml
[prompts]
reading = "my-prompts/reading.md"
review = "my-prompts/review.md"
```

Prompt keys are `system`, the six method names, `repair_draft`, and `review`. Method parameters are included in the request. A prompt change requires a new output directory.

Add `method_files = ["path/to/method.py"]` at the top of a config, before TOML tables. A method file defines a unique lower-case `name`, `description`, and `generate(source, context, params)`. It returns the common candidate result. `preview(source, context, params)` is optional. The [example method](generation/example_method.py) demonstrates this interface; `python3 -B -m oaimathdistill.generation list --method-file oaimathdistill/generation/example_method.py` loads it without generating data.

Method files are ordinary trusted local Python code: import and custom `preview` code execute when loaded. Keep model calls and other side effects inside `generate`, using `context.call` so that trajectories and checkpoints are retained. Add a suitable custom review rubric through `[prompts].review` when the new method needs criteria beyond the existing ones. Registered method files are hashed; external helper modules that a plugin imports are not automatically fingerprinted, so keep the reproducible method logic in those registered files or explicitly record its additional dependencies.

Use `context.call(stage, prompt, schema, validator=...)` for each model call. A stable stage name gives a separate checkpoint; names beginning with `review_` are reserved for the runner. Calls return parsed JSON; supply a validator for additional semantics before checkpointing. The supported local JSON Schema subset is documented in `generation/core.py`. `context.generation_prompt`, `source.context_text`, `source.check_evidence`, and `methods.common.check_result` provide reusable helpers. The repair method is a working multi-call example. Custom methods control their own student instruction; include source context there when the custom task requires it. The runner automatically attaches context for the built-in reading, reflection, and exploration methods.

Each model-produced candidate contains `instruction`, `answer`, `evidence`, `basis`, and `uncertainty`. Evidence entries contain `path`, `start_line`, and `end_line`. The runner checks locations against selected original files and fragment ranges, preserving raw file and excerpt SHA-256 hashes. Location validity does not establish that a citation entails the answer.

### Outputs, review, and recovery

Each run contains:

| Artifact | Meaning |
| --- | --- |
| `run.json` | Effective project configuration, source snapshots, implementation hashes, CLI version, and run fingerprint. |
| `jobs/<id>/steps/<stage>/` | Per-call requests, attempts, raw output/events, usage when reported, and valid-response checkpoints. |
| `traces.jsonl` | Index of every generation/review call attempt, including failures and retries, with relative links to its artifacts. |
| `jobs/<id>/result.json` | Completed, empty, or failed job, including available candidates and reviews. |
| `candidates.jsonl` | All structurally accepted generated candidates, including rejected or unresolved ones. |
| `reviews.jsonl` | Separate model assessments and stated evidence scope. |
| `outcomes.jsonl` | Job statuses, empty reasons, and execution failures. |
| `train.jsonl` | Deduplicated conversations from completed jobs with passed model reviews and program checks. |
| `summary.json`, `report.md` | Counts and the execution/evidence boundary. |

### Random prompt seeds

`run.prompt_seed` defaults to `true`. Each real `context.call` attempt receives a newly generated 32-character hexadecimal string, appended as `Random seed: <string>` on its own prompt line. This applies to generation, repair stages, and independent review requests through either backend. The generation prompt treats it as a cue to vary supported topics, task wording, or reasoning steps; correctness and grounding requirements stay the same. Review criteria remain unchanged. The prompt instructs the model to keep the seed out of student messages; a response that copies a current construction seed into an instruction or answer is rejected before checkpointing. The seed is not a provider numeric decoding parameter or a guarantee of output diversity.

Seeds are created only when starting actual calls, and saved before invoking the backend in `request.json` and `trace.json`. A successful checkpoint records its seed and exact request signature; candidate metadata retains `generation_seeds` by stage and `review_seed`. Empty and failed jobs retain attempt-level seed records too. `traces.jsonl` exposes each attempt's seed and request signature.

Completed checkpoints resume with their original seed and response without new randomness or model calls. A failed step retried with `--retry-failed` is a new request with a fresh seed, while its completed prerequisites keep their seeds and every earlier attempt stays available. Seed/request inconsistencies in a checkpoint are rejected. A fresh output directory permits a new run with new seeds; code, source, prompt, and configuration identity remain separate from attempt randomness. Preview uses a fixed placeholder and writes no seed files. Set `prompt_seed = false` to disable this feature.

### Generation and review trajectories

Every model call, including intermediate construction stages and separate review requests, gets an `attempt-NNN/` directory. It retains:

- `request.json`: the exact seeded prompt, output schema, backend name, random seed, stable base signature, and actual request signature.
- `trace.json`: purpose (`generation` or `review`), backend kind, request signature, UTC start/end times, elapsed seconds, and application status. A hard process kill may leave status `started` and no end time.
- For Codex, `invocation.json`, `schema.json`, `events.jsonl`, `stderr.txt`, and `answer.json` when returned. The event stream preserves the CLI's exposed messages, tool calls, tool results, and warnings in their original order.
- For APIs, `http_request.json` and `http_response.json`, or `http_error.txt` for HTTP errors. The complete returned JSON is retained, including provider-specific fields and any returned reasoning content or summaries. Authorization headers are not saved, and the configured credential is redacted from stored HTTP responses/errors.
- `response.json` for parsed results, `usage.json` when the response reports usage, and `error.json` for failed/interrupted calls. A valid response also gets a stage-level `checkpoint.json`.

Trajectory retention is always enabled for both generation and review. Only content actually exposed by the backend can be recorded; these files do not imply access to hidden internal reasoning. Trajectories remain separate from the student `messages` export and can later be processed explicitly for another training format.

The runner writes `traces.jsonl` at normal completion and when `export` is invoked. Its rows identify the job, source, method, stage, backend, attempt, status, and the checkpoint selected for reuse. All artifact paths are relative to the run directory. If a run was interrupted, its per-attempt files remain available; `export` rebuilds the index without model calls. Older runs can be indexed without changing their raw files: missing lifecycle metadata stays unknown, while checkpoints/errors establish the recorded application outcome. An application failure can still contain a useful model response; inspect the raw artifacts before deciding to retry.

### Export and recovery

The exported `messages` contain only the student task and intended answer. Metadata records source, method, reading form in `params`, fingerprint, checks, and review. A flawed proof appears in the student's correction task, not as a positive assistant target. Exploration is reviewed for grounding, motivation, and uncertainty rather than requiring its open ideas to be proved.

The same command resumes completed calls and jobs. Failed or uncertain calls are not automatically retried. Inspect their artifacts, then pass `--retry-failed` to attempt unfinished stages; completed earlier stages are reused. Each attempt is preserved. There is no guarantee of exactly-once remote billing if a process dies after a request was accepted but before its response checkpoint was saved.

Changes to source content, method code, prompts, or configuration require a new output directory. `--output runs/new-name` selects one. The runner rechecks registered implementation/prompt files and the loaded configuration before and after model calls, rejecting changes instead of silently mixing versions under one fingerprint. Responses already received remain available for inspection.

The same run lock covers both `run` and `export`, preventing concurrent writers. After an unclean process kill, verify its owner has stopped before removing a stale `.run.lock`. Ctrl-C terminates active CLI process groups and retains completed checkpoints. Closing the HTTP backend blocks subsequent requests; an in-flight HTTP request may still need to finish or reach its configured socket timeout before shutdown completes. After the writer stops, `export` can rebuild artifacts without starting a model request.

Source claims, model review, and independent mathematical checking remain distinct. Built-in program checks cover response shape and source identity/locations, not a general proof verifier. No training benefit follows from a successful generation run.

Read source material from the pinned `math/` snapshot and write generated artifacts outside it. Preserve enough provenance to locate the original material and reproduce each transformation.
