#!/usr/bin/env python3
"""Create the private local project files from a plain-text or HTML manuscript."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"


class ParagraphParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_paragraph = False
        self.buffer: list[str] = []
        self.paragraphs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "p":
            self.in_paragraph = True
            self.buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag != "p" or not self.in_paragraph:
            return
        text = re.sub(r"\s+", " ", "".join(self.buffer).replace("\xa0", " ")).strip()
        if text:
            self.paragraphs.append(text)
        self.in_paragraph = False
        self.buffer = []

    def handle_data(self, data: str) -> None:
        if self.in_paragraph:
            self.buffer.append(data)


def manuscript_paragraphs(source: Path) -> list[str]:
    text = source.read_text(encoding="utf-8")
    if source.suffix.lower() in {".html", ".htm"}:
        parser = ParagraphParser()
        parser.feed(text)
        return parser.paragraphs
    return [re.sub(r"\s+", " ", part).strip() for part in re.split(r"\n\s*\n", text) if part.strip()]


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def build_project_source(source: Path, title: str, author: str, language: str, target_chars: int) -> tuple[dict, dict]:
    paragraphs = manuscript_paragraphs(source)
    if not paragraphs:
        raise ValueError("The manuscript contains no paragraphs.")
    narration = "\n\n".join(paragraphs)
    narration_sha256 = hashlib.sha256(narration.encode("utf-8")).hexdigest()
    paragraph_records = [
        {"id": f"p{index:04d}", "text": text, "chars": len(text)}
        for index, text in enumerate(paragraphs, 1)
    ]
    paragraph_ids = [item["id"] for item in paragraph_records]
    story = {
        "version": 2,
        "title": title,
        "author": author,
        "language": language,
        "source": {
            "path": str(source.resolve()),
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "narration_sha256": narration_sha256,
        },
        "stats": {
            "paragraphs": len(paragraph_records),
            "characters": sum(item["chars"] for item in paragraph_records),
            "utterances": 0,
        },
        "paragraphs": paragraph_records,
        "beats": [{
            "id": "b001",
            "title": "Manuscript",
            "start": paragraph_ids[0],
            "end": paragraph_ids[-1],
            "paragraph_ids": paragraph_ids,
            "suggested_emotion": "",
            "reason": "Initial neutral pass.",
        }],
        "utterances": [],
    }
    annotations = {
        "version": 3,
        "story_sha256": narration_sha256,
        "include_title": True,
        "target_chars": target_chars,
        "global": {"emotion": "", "expressiveness": "expressive_low"},
        "utterances": {},
    }
    return story, annotations


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="UTF-8 .txt, .md, or .html manuscript")
    parser.add_argument("--title", required=True)
    parser.add_argument("--author", default="")
    parser.add_argument("--language", default="en")
    parser.add_argument("--target-chars", type=int, default=380)
    parser.add_argument("--force", action="store_true", help="replace existing local project source files")
    args = parser.parse_args()

    targets = [DATA_DIR / "story.json", DATA_DIR / "annotations.json"]
    if not args.force and any(path.exists() for path in targets):
        raise SystemExit("Local project files already exist. Use --force only after making a backup.")
    if not 220 <= args.target_chars <= 700:
        raise SystemExit("--target-chars must be between 220 and 700.")
    story, annotations = build_project_source(
        args.source.resolve(), args.title.strip(), args.author.strip(), args.language.strip(), args.target_chars
    )
    if not story["title"]:
        raise SystemExit("--title cannot be empty.")
    atomic_json(targets[0], story)
    atomic_json(targets[1], annotations)
    print(f"Created {story['title']!r}: {story['stats']['paragraphs']} paragraphs, {story['stats']['characters']} characters.")
    print("Copy voices.example.json to voices.local.json, add a canonical reference, then run ./run.command.")


if __name__ == "__main__":
    main()
