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

The pack includes tracked `.py` files, small config files (`pyproject.toml`, `requirements*.txt`, `setup.cfg`, `README*`, `.ini`, `.toml`, `.json`, `.yaml`, `.yml`) and files under 200 KB by default. `.env` and binary files are always excluded. A regex secret scan refuses to build the pack unless `--mask` is given.

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

1. Ask the Gem for a change. It answers with a short plan and one or more edit blocks.
2. Copy the whole answer to the clipboard.
3. From the target repository run:

   ```bash
   python3 /path/to/MiniCodeAssist/loop.py
   ```

4. The tool applies the edits, runs checks, prints the report and copies the report to the clipboard.
5. Paste the report back into the Gem. Repeat until the task is done.

If you cannot use the clipboard (for example in a plain SSH session), save the answer to a file:

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

An empty SEARCH creates a new file. SEARCH must be copied exactly and must be unique in the file. Matching has three tiers: exact, ignoring trailing whitespace/CRLF, then ignoring common indentation. Line endings of the target file are preserved. Ambiguous or missing SEARCH text is rejected with the closest matching lines.

## Loop options

| Option | Meaning |
| --- | --- |
| `--repo PATH` | Target repository (default: current directory). |
| `--file PATH` | Read the Gem response from a file instead of the clipboard. |
| `--undo` | Restore the files touched by the last apply pass. |
| `--run` | Run `python3 main.py` interactively first and include its output in the report. Can also be used on its own to capture a crash without applying edits. Use this only on purpose. |
| `--repack` | Rebuild the repository pack. |
| `--no-checks` | Skip compilation, import and unittest checks. |

Backups of touched files are stored in `.mca/backup/<timestamp>/`. `.mca/` is added to `.git/info/exclude` automatically, so none of this pollutes git status.

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
- Paths escaping the repository, absolute paths and edits to `.mca/` are refused.
- The pack is blocked when a likely secret is detected; use `--mask` deliberately if you need to proceed.
- The Gem may make mistakes. Review `git diff` before committing.

## Development

Run the test suite from this directory:

```bash
python3 -m unittest discover -s tests -v
```

The project has no third-party dependencies and no lint/type-check configuration.
