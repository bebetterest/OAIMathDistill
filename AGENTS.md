# Project instructions

- Write project documentation, diary entries, code comments, and public artifacts in English.
- Keep the upstream `math/` snapshot reproducible. Processing code should read it and write derived artifacts elsewhere.
- Put project code in `oaimathdistill/`. Add dependencies when a concrete processing or checking step needs them.
- Keep diaries, process notes, experiment logs, review reports, and rename records local under ignored `diary/`, `runs/`, or `logs/` directories. Do not commit these records or their local backups.
- Public documentation should explain current behavior, configuration, provenance, and limitations. Keep local execution history in the ignored records.
- Preserve provenance and the scope of checks. A source's claim, a supplied proof, a successful local check, and a demonstrated training benefit are different kinds of evidence.
- Start with small working experiments. Extend shared abstractions when real use cases establish the need.
- When creating commits, preserve the user's configured primary author and append `Co-Authored-By: GPT <actual model number> <codex@openai.com>` to the commit message. Use the actual model for that commit, not a fixed example model.
