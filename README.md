# MiniCodeAssist

MiniCodeAssist is a small, standard-library-only bridge between a Gemini Gem and a local Python repository. It packs a git repository into one Markdown file that you attach to the Gem, then applies the Gem's SEARCH/REPLACE edits locally, runs bounded checks, and copies a short report back to the clipboard for the next chat turn.

No API keys, no pip packages, no network calls.

## Requirements

- Python 3.10 or newer.
- A target project under git, ideally under 10,000 lines, started with `python3 main.py`.
- On WSL: `clip.exe` and `powershell.exe` on the Windows side. On native Linux: `wl-copy`/`wl-paste` or `xclip` (optional if you use `--file`).

## 1. Install

Copy this directory anywhere. No installation step is required.

## 2. Set up the Gem

1. Open the target repository: `cd /path/to/target-repo`.
2. Build the pack:

   ```bash
   python3 /path/to/MiniCodeAssist/pack_repo.py --canary
   ```

   This writes `.mca/repo_pack.md` in the target repository and, under WSL, copies it to your Windows Downloads folder.
3. Start a new conversation with your Gem. Paste the contents of `gem_instructions.md` into the Gem instruction field (adjust the "EDITABLE LINE" for the explanation language).
4. Attach `repo_pack.md` to the conversation.

Repeat step 2 and start a fresh conversation whenever the code has changed substantially.

### Packer options

| Option | Meaning |
| --- | --- |
| `--repo PATH` | Target repository (default: current directory). |
| `--canary` | Insert three integrity markers at the start, middle and end. |
| `--mask` | Replace suspected secret values with `***MASKED***` instead of refusing. |
| `--out PATH` | Custom output path instead of `.mca/repo_pack.md`. |
| `--ext txt` | Include an extra file extension (repeatable). |
| `--no-downloads-copy` | Do not copy the pack to the Windows Downloads folder. |

The pack includes tracked and untracked (but not git-ignored) `.py` files, small config files (`pyproject.toml`, `requirements*.txt`, `setup.cfg`, `README*`, `.ini`, `.toml`, `.json`, `.yaml`, `.yml`) and files under 200 KB by default. `--ext` and `extra_extensions` accept `yaml` or `.yaml`. Binary files and files that usually hold credentials are always excluded: `.env*` (except `.env.example`/`.sample`/`.template`), `credentials.*`, `secrets.*`, `service-account*`, `*.pem`, `*.key`, `*.p12`, `.netrc`, `.pypirc`, SSH keys. A regex secret scan (quoted assignments such as `DB_PASSWORD = "..."`, unquoted values in config files, `sk-...`, GitHub, AWS, Slack, Google, Stripe tokens, URLs with passwords, private keys) refuses to build the pack unless `--mask` is given. The scan is a safety net, not a guarantee.

A rough token estimate is printed after packing.

### Per-repository configuration

Create `.mca.ini` in the target repository:

```ini
[mca]
max_file_kb = 200
test_dir = tests
extra_extensions = pyi, yaml
downloads_dir = /mnt/c/Users/me/Downloads
```

All keys are optional.

## 3. Round trip

1. Ask the Gem for a change. It answers with a short plan in the chat and a downloadable `gem_response.txt` that holds the edit blocks.
2. Download `gem_response.txt` (if the Gem could not produce a file, copy its whole answer to the clipboard instead).
3. From the target repository run:

   ```bash
   python3 /path/to/MiniCodeAssist/loop.py --file /path/to/gem_response.txt
   ```

   Without `--file`, `loop.py` reads the clipboard.

4. The tool applies the edits, runs checks, prints the report and copies the report to the clipboard.
5. Paste the report back into the Gem. Repeat until the task is done.

If you cannot use the clipboard (for example in a plain SSH session), save the answer to a file and pass it with `--file`:

```bash
python3 /path/to/MiniCodeAssist/loop.py --file reponse.md
```

The report is always saved to `.mca/last_report.md`.

## Edit format

```
### FILE: relative/path.py
<<<<<<< SEARCH
exact existing lines
to replace
=======
new lines
that replace them
>>>>>>> REPLACE
```

An empty SEARCH creates a new file. SEARCH must be copied exactly, must cover whole lines and must be unique in the file. Every block needs its own `### FILE:` header, even for several blocks in the same file. Matching has three tiers: exact (whole lines only), ignoring trailing whitespace/CRLF, then ignoring common indentation. Line endings of the target file are preserved. Ambiguous or missing SEARCH text is rejected with the closest matching lines.

Blocks are applied all-or-nothing per file: if one block for a file is rejected, that file is left unchanged and the other files are still applied. Backups are written before any file is modified. Non-UTF-8 files are rejected instead of crashing.

## Loop options

| Option | Meaning |
| --- | --- |
| `--repo PATH` | Target repository (default: current directory). |
| `--file PATH` | Read the Gem response from a file instead of the clipboard. |
| `--undo` | Restore the files touched by the last apply pass. |
| `--run` | Run `python3 main.py` interactively and include its output in the report. On its own it does not read the clipboard and applies nothing (useful to capture a crash); combine with `--file` to apply edits first. Use this only on purpose. |
| `--repack` | Rebuild the repository pack. |
| `--no-checks` | Skip compilation, import and unittest checks. |
| `--force` | Apply a response even if the same response was already applied. |

A hash of the last applied response is kept in `.mca/last_applied.sha256`, so running `loop.py` twice on the same clipboard does not apply the edits twice. `--undo` clears it.

Exit codes: `0` everything applied and all checks passed, `1` some blocks were rejected or a check failed, `2` the response or configuration could not be used.

Backups of touched files are stored in `.mca/backup/<timestamp>/`. `.mca/` is added to `.git/info/exclude` automatically (by the packer and by the loop), so none of this pollutes git status.

## Checks and reports

Each pass reports:

- applied and rejected blocks, with line numbers and hints;
- `py_compile` for changed Python files;
- an import check for changed modules without top-level side effects;
- `python3 -m unittest discover` when a `tests/` directory exists;
- `git diff --stat`;
- optional `main.py` run output when `--run` was used.

Reports are bounded in size and tracebacks are trimmed to repository frames plus the final error. Secrets are masked in report output.

## Safety notes

- `main.py` is never executed automatically. Only `--run` starts it, and output is shown live on your terminal.
- The checks do run code written by the Gem: changed modules are imported (unless top-level side effects are detected, including assignments that call functions) and `unittest discover` runs your tests. Use `--no-checks` and review `git diff` first if you do not trust an answer.
- Paths escaping the repository, absolute paths and edits under `.git/` or `.mca/` are refused (case-insensitively, to cover Windows filesystems under WSL).
- The pack is blocked when a likely secret is detected; use `--mask` deliberately if you need to proceed.
- The Gem may make mistakes. Review `git diff` before committing.

## Development

Run the test suite from this directory:

```bash
python3 -m unittest discover -s tests -v
```

The project has no third-party dependencies and no lint/type-check configuration.
