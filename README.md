# Borg Queen Control

Local inbox assimilation for an Obsidian vault. It runs on any machine with Python 3, SQLite FTS5, and Ollama. A Ryzen 9 is not required. Pick smaller models in the reasoning-core menu when VRAM is tight.

## What you need

- Python 3 with SQLite FTS5
- [Ollama](https://ollama.com) running at `http://localhost:11434`
- Models you actually have installed. The defaults are `qwen2.5vl:7b` (scout), `olmo-3:7b-think` (cross-link), `zarigata/unfiltered-llama3:latest` (adversary), and `cogito:32b` (queen). Change them in the control if those tags are not on this machine.
- PyMuPDF, installed by `install.sh`, so inbox PDFs become Markdown with tables and figure images

## Install

```bash
chmod +x install.sh
./install.sh
borg-queen
```

Then open `http://localhost:8082`.

`install.sh` copies the server to `~/.borg_queen`, creates the vault folders, and adds a `borg-queen` command.

## Vault location

By default:

- Wiki: `~/Documents/PATL/Lab_Second_brain/wiki`
- Inbox: `~/Documents/PATL/Lab_Second_brain/inbox`

Override either path:

```bash
export BORG_VAULT=/path/to/wiki
export BORG_INBOX=/path/to/inbox
```

Drop `.md` or `.pdf` files in the inbox and click **ASSIMILATE INBOX STUDIES**. A PDF is converted to Markdown first. Tables become Markdown tables. Embedded images and chart pages are saved beside the note and moved into the wiki with it.

## Run without the installer

```bash
python3 -m pip install -r requirements.txt
python3 server.py --port 8082
```
