# LEPT Transcript and Exam Generator

This local FastAPI application extends the existing YouTube transcript extractor with an approval-gated question-generation workflow powered by Ollama.

## One-click start

Double-click `Start LPT Reviewer.cmd` in the project root. The launcher creates the virtual environment when needed, installs missing Python packages, starts Ollama if necessary, checks for `qwen3:8b`, starts the local server, and opens the reviewer in your browser.

The launcher does not download Ollama models automatically. If the model is missing, run `ollama pull qwen3:8b` once and start the launcher again.

## Requirements

- Windows 11
- Python 3.10 or newer
- Node.js (used for final JavaScript validation)
- Ollama running locally at `http://localhost:11434`
- Ollama model `qwen3:8b`
- Internet access while fetching YouTube captions

Install the Ollama model manually if needed:

```powershell
ollama pull qwen3:8b
```

The application never downloads a model automatically.

## Setup

Open PowerShell in this folder:

```powershell
cd "C:\Users\Paul Emmanuel\Desktop\LPT Reviewer\tools\transcript-generator"
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If activation is blocked for the current PowerShell window:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
```

## Run

```powershell
cd "C:\Users\Paul Emmanuel\Desktop\LPT Reviewer\tools\transcript-generator"
.\.venv\Scripts\Activate.ps1
uvicorn app:app --reload
```

Open <http://127.0.0.1:8000>.

## Workflow

1. Enter a YouTube URL and session details.
2. Extract the available captions.
3. Review and edit the transcript.
4. Save the edited transcript.
5. Generate draft questions with local `qwen3:8b`.
6. Review each question's topic key and source-evidence excerpt; edit, delete, or regenerate individual questions.
7. Run deterministic validation.
8. Optionally validate source grounding with Ollama.
9. Download a standalone JavaScript copy if desired.
10. Explicitly approve the draft and confirm the final action.
11. The server backs up `melvin-questions.js`, surgically appends the session, and validates the final JavaScript.

## Storage

- Edited transcripts: `generated/transcripts/`
- Draft JSON and standalone JavaScript: `generated/questions/`
- Production-bank backups: `generated/backups/`

These generated folders are ignored by Git to reduce the risk of publishing private transcripts or draft material.

## Safety behavior

- Generation never modifies `melvin-questions.js`.
- Generation first builds a grounded topic pool and reports when the transcript has too few distinct testable concepts.
- Drafts retain internal `topic_key`, `source_evidence`, and validation-override metadata; exports strip those fields and keep the five-field reviewer tuple.
- Correct-answer positions are redistributed deterministically without changing the correct answer text.
- Deterministic checks flag conceptual overlap, unsupported evidence or explanations, invalid sections, trivial option variants, and uneven answer positions.
- AI validation reports separate source, answer, explanation, section, uniqueness, and option judgments and suggests a section when needed.
- Saving or validating a draft never modifies `melvin-questions.js`.
- Downloading JavaScript never modifies `melvin-questions.js`.
- The approval endpoint requires `confirmed: true`.
- Approval repeats deterministic validation and rejects duplicate banks or prefixes.
- A timestamped backup must succeed before production is changed.
- The candidate JavaScript is syntax-checked and executed in a separate Node.js process before replacement.
- The completed bank is checked again after replacement.
- If final validation fails, the backup is restored automatically.

## Ollama errors

- If Ollama is unavailable: `Ollama is not running. Start Ollama and try again.`
- If the model is missing: `qwen3:8b is not installed. Run: ollama pull qwen3:8b`

No OpenAI API or paid API is used.
