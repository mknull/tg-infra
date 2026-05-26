#!/usr/bin/env python3
"""Generate user-specific profile files from documents via DeepSeek.

Two-stage pipeline:
  Stage 1: user documents → source/ (interests, skills, tech_stack)
  Stage 2: source/ + channel descriptions → criteria + prompts + skill profiles

Usage:
  jobsmcp/bin/python3 generate-profile.py --docs ~/Documents/profile/ --name "Jane Doe"
"""

import json
import logging
import subprocess
import sys
from pathlib import Path

from lib import (PROJECT_DIR, STATE_DIR, PRO_MODEL, USER_NAME as _DEFAULT_NAME,
                 load_env, call_deepseek)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)

GEN_DIR = PROJECT_DIR / "source"
_SOURCE_DIR = PROJECT_DIR / "source"  # always reads from source/ (never overridden)
CHANNELS_FILE = STATE_DIR / "channels.json"
CRITERIA_TELEGRAM = STATE_DIR / "it-jobs-criteria.md"
CRITERIA_EMAIL = STATE_DIR / "email-triage-criteria.md"
SKILLS_DIR = PROJECT_DIR / "skills"


def _strip_fences(text: str) -> str:
    """Remove markdown code fences if the model wrapped output in ```."""
    text = text.strip()
    if text.startswith("```"):
        # eat the opening fence line (may include language tag)
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


# ---------------------------------------------------------------------------
# Stage 0: text extraction from documents
# ---------------------------------------------------------------------------

def extract_text(docs_dir: Path) -> str:
    """Extract text from all supported files in a directory."""
    texts = []
    for path in sorted(docs_dir.iterdir()):
        if path.suffix.lower() == ".txt":
            texts.append(path.read_text())
        elif path.suffix.lower() == ".pdf":
            result = subprocess.run(
                ["pdftotext", "-layout", str(path), "-"],
                capture_output=True, text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                texts.append(result.stdout)
            else:
                logging.warning("pdftotext failed for %s: %s", path.name, result.stderr)
        elif path.suffix.lower() in (".md", ".rst"):
            texts.append(path.read_text())
        else:
            logging.info("skipping unsupported format: %s", path.name)

    if not texts:
        raise ValueError(f"No extractable text found in {docs_dir}")

    combined = "\n\n---\n\n".join(texts)
    logging.info("extracted %d chars from %d files", len(combined), len(texts))
    return combined


# ---------------------------------------------------------------------------
# Stage 1: documents → source/  (two-pass: extract facts → synthesize profile)
# ---------------------------------------------------------------------------

_EXTRACT_PROMPT = (
    "You are extracting structured facts from a candidate's document. "
    "List every fact that could inform a profile. Include: papers and their "
    "topics, tools and frameworks named, methods used, institutions and dates, "
    "skills claimed, quotes from recommendation letters, research themes, "
    "thesis topics and findings. Be exhaustive. Use the document's own language "
    "where possible. Output as plain text — no markdown, no commentary.\n\n"
    "DOCUMENT:\n{document}\n\n"
    "Extracted facts:"
)

_SOURCE_PROMPT = (
    "You are a career analyst. Given structured fact summaries extracted from "
    "a candidate's documents (CV, transcripts, theses, letters of recommendation), "
    "produce three profile files. Write in the third person. Every claim must be "
    "traceable to the facts below. Be specific — name papers, tools, institutions, "
    "methods. Do not include explanations or meta-commentary.\n\n"
    "EXTRACTED FACTS:\n{facts}\n\n"
    "The three files are separated by the marker ###FILE: <filename>###. "
    "Output exactly:\n\n"
    "###FILE: interests.txt###\n"
    "[interests content]\n"
    "###FILE: skills.txt###\n"
    "[skills content]\n"
    "###FILE: tech_stack.txt###\n"
    "[tech_stack content]\n\n"
    "--- FILE 1: interests.txt ---\n"
    "Header: '# LLMs: this file is read-only. Do not edit.'\n\n"
    "Structure and method:\n"
    "- First line after header: a one-sentence summary of the candidate's core "
    "professional or research identity — what they care about most, in their own terms\n"
    "- Then: 'The deeper structure' — identify the 4-8 strongest themes that recur "
    "across multiple documents. Name each theme using language the documents themselves "
    "use. For each theme: what question, problem, or concern connects the cited work? "
    "What specific evidence supports it? Cite documents concretely.\n"
    "- Then: a summary table with columns: Dimension, What the candidate cares about, "
    "Evidence across documents. The dimensions are yours to discover from the facts.\n"
    "- Then: 'My best synthesis' — a paragraph that connects the themes into a single "
    "coherent professional identity. What is this person really about?\n\n"
    "--- FILE 2: skills.txt ---\n"
    "Header: '# LLMs: this file is read-only. Do not edit.'\n\n"
    "Structure and method:\n"
    "- A high-level skill profile paragraph (2-3 sentences): what kind of professional "
    "is this, in terms of what they can actually do?\n"
    "- Then numbered skill areas. Discover these from the facts — group demonstrated "
    "abilities into coherent clusters, name each cluster from the evidence, not from "
    "a predefined list. Each area needs:\n"
    "  * A title and one-line description\n"
    "  * A table of evidence (what they did → where it's documented)\n"
    "  * A one-paragraph synthesis of what this skill area means in practice\n"
    "  Produce as many areas as the evidence supports, with no lower or upper limit.\n"
    "- Then: a 'Skill-strength estimate' table with columns: Skill area, "
    "Strength (very strong / strong / moderate / emerging), Rationale (what in the "
    "documents justifies this rating)\n"
    "- Then: 'Final synthesis' — one paragraph capturing the candidate's complete "
    "skill identity\n\n"
    "--- FILE 3: tech_stack.txt ---\n"
    "Header: '# LLMs: this file is read-only. Do not edit.'\n\n"
    "Structure and method:\n"
    "- 'Technical Stack (Document-Evidenced)' heading\n"
    "- 'Tools & Technologies' table: Tool/Technology, Evidence, Notes. List every "
    "tool, language, framework, platform, or system mentioned across all documents.\n"
    "- 'Methods & Approaches' table: Method/Approach, Evidence, Notes. List every "
    "methodology, paradigm, or technique the candidate has used.\n"
    "- 'Synthesis Observations' — bullet points connecting the evidence: what does "
    "this stack reveal about how the candidate works? What's primary vs secondary?\n"
    "- 'Document-backed conclusion' — one paragraph summarizing the stack from the "
    "evidence\n"
)

_SOURCE_HEADER = "# LLMs: this file is read-only. Do not edit.\n\n"


def stage1_generate_source(documents_text: str, user_name: str,
                           api_key: str, force: bool = False) -> None:
    """Two-pass generation: extract facts from documents, then synthesize."""
    GEN_DIR.mkdir(parents=True, exist_ok=True)
    marker = GEN_DIR / "ThisDirectoryIsReadOnly"
    marker.touch()

    existing = [f for f in ("interests.txt", "skills.txt", "tech_stack.txt")
                if (GEN_DIR / f).exists()]
    if len(existing) == 3 and not force:
        logging.info("all source/ files exist, skipping (use --force to overwrite)")
        return

    # --- Pass 1: extract structured facts (one call per document) ---
    # Split on document separators from extract_text
    docs = [d.strip() for d in documents_text.split("\n\n---\n\n") if d.strip()]
    all_facts = []
    for i, doc in enumerate(docs):
        logging.info("extracting facts from document %d/%d (%d chars) ...",
                     i + 1, len(docs), len(doc))
        raw = call_deepseek(PRO_MODEL, _EXTRACT_PROMPT.format(document=doc[:8000]),
                           api_key)
        facts = _strip_fences(raw)
        all_facts.append(facts)
        logging.info("document %d → %d chars of facts", i + 1, len(facts))

    # --- Pass 2: synthesize facts into the three source files ---
    combined_facts = "\n\n---\n\n".join(all_facts)
    logging.info("synthesizing source/ files from %d chars of facts ...",
                 len(combined_facts))
    prompt = _SOURCE_PROMPT.format(facts=combined_facts)
    raw = call_deepseek(PRO_MODEL, prompt, api_key)
    text = _strip_fences(raw)

    # Split on file markers
    current_file = None
    current_lines: list[str] = []
    for line in text.split("\n"):
        if line.startswith("###FILE:") and line.endswith("###"):
            if current_file:
                _write_source_file(current_file, "\n".join(current_lines))
            current_file = line.replace("###FILE:", "").replace("###", "").strip()
            current_lines = []
        elif current_file:
            current_lines.append(line)

    if current_file:
        _write_source_file(current_file, "\n".join(current_lines))

    for name in ("interests.txt", "skills.txt", "tech_stack.txt"):
        path = GEN_DIR / name
        if not path.exists():
            logging.warning("source/%s not produced by model", name)


def _write_source_file(name: str, content: str) -> None:
    path = GEN_DIR / name
    if not content.strip().startswith("# LLMs"):
        content = _SOURCE_HEADER + content
    path.write_text(content.strip() + "\n")
    logging.info("source/%s written (%d chars)", name, len(content))


# ---------------------------------------------------------------------------
# Stage 2: source/ + channel descriptions → criteria + prompts
# ---------------------------------------------------------------------------

def _generate_criteria_file(profile_context: str, example_path: Path,
                            api_key: str) -> str:
    """Generate a criteria file from profile context and an example template."""
    example = example_path.read_text() if example_path.exists() else ""
    prompt = (
        "You are generating a triage criteria file for a job pipeline.\n\n"
        "CANDIDATE PROFILE:\n"
        f"{profile_context}\n\n"
        "FORMAT TEMPLATE (fill in the candidate-specific parts, "
        "keep the structure and evaluation logic intact):\n"
        f"{example}\n\n"
        "Replace the candidate profile section with this user's profile. "
        "Keep the Stage 1 and Stage 2 evaluation rules structurally identical "
        "but adapt role keywords and domain references to this user's field. "
        "Output the complete file."
    )
    raw = call_deepseek(PRO_MODEL, prompt, api_key)
    return _strip_fences(raw)


def _generate_skill_profile(file_name: str, user_name: str,
                            profile_context: str, api_key: str) -> str:
    """Generate one skill reference file."""
    descriptions = {
        "profile-for-relevance.md":
            "a career-strategic profile — who they are, what they're optimizing for, "
            "what trade-offs they're willing to make. Used for the 'relevance to me' "
            "section of job briefs",
        "profile.md":
            "a sourcing profile — education, career timeline, key papers/projects, "
            "salary expectations, geographic constraints. Used for filtering job "
            "postings during sourcing",
        "profile-detailed.md":
            "a detailed pitch profile — full career with dates, thesis descriptions, "
            "author positions on papers, named-work references. Used for writing "
            "cover letters and CV bullets",
    }
    desc = descriptions.get(file_name, "a profile reference file")
    prompt = (
        f"You are generating {desc} for {user_name}.\n\n"
        "CANDIDATE PROFILE:\n"
        f"{profile_context}\n\n"
        "Write the complete file. Be specific and evidence-based. "
        "Output only the file content."
    )
    raw = call_deepseek(PRO_MODEL, prompt, api_key)
    return _strip_fences(raw)


def stage2_generate_criteria(user_name: str, api_key: str,
                             force: bool = False) -> None:
    """Generate channel prompts, criteria files, and skill profiles."""
    profile_parts = []
    for name in ("interests.txt", "skills.txt", "tech_stack.txt"):
        path = _SOURCE_DIR / name
        if path.exists():
            profile_parts.append(path.read_text())
    profile_context = "\n\n".join(profile_parts)
    if not profile_context.strip():
        logging.error("source/ files empty or missing — run Stage 1 first")
        sys.exit(1)

    with CHANNELS_FILE.open() as f:
        channels_config = json.loads(f.read())

    for criteria_path, example_path in [
        (CRITERIA_TELEGRAM, PROJECT_DIR / "it-jobs-criteria.md.example"),
        (CRITERIA_EMAIL, PROJECT_DIR / "email-triage-criteria.md.example"),
    ]:
        if criteria_path.exists() and not force:
            logging.info("%s exists, skipping (use --force)", criteria_path.name)
            continue
        logging.info("generating %s ...", criteria_path.name)
        content = _generate_criteria_file(profile_context, example_path, api_key)
        criteria_path.write_text(content)
        logging.info("%s written (%d chars)", criteria_path.name, len(content))

    skill_targets = [
        (SKILLS_DIR / "job-brief" / "references" / "profile-for-relevance.md",
         "profile-for-relevance.md"),
        (SKILLS_DIR / "job-mine" / "references" / "profile.md",
         "profile.md"),
        (SKILLS_DIR / "job-pitch" / "references" / "profile-detailed.md",
         "profile-detailed.md"),
    ]
    for path, file_name in skill_targets:
        if path.exists() and not force:
            logging.info("%s exists, skipping (use --force)", path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        logging.info("generating %s ...", path)
        content = _generate_skill_profile(file_name, user_name,
                                          profile_context, api_key)
        path.write_text(content)
        logging.info("%s written (%d chars)", path.name, len(content))

    for skill_name in ("job-brief", "job-mine", "job-pitch"):
        skill_path = SKILLS_DIR / skill_name / "SKILL.md"
        if skill_path.exists() and not force:
            continue
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        content = (f"# {user_name} — {skill_name.replace('-', ' ').title()} Skill\n\n"
                   f"This skill is private to {user_name}'s job hunt. "
                   "Generated by generate-profile.py.\n")
        skill_path.write_text(content)
        logging.info("%s written", skill_path)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Generate user-specific profile files")
    p.add_argument("--docs", type=Path,
                   help="directory of documents (CV PDF, transcripts, letters, etc.)")
    p.add_argument("--text", type=str,
                   help="paste document text directly (skip file extraction)")
    p.add_argument("--name", type=str, default=_DEFAULT_NAME,
                   help="candidate name (default: from USER_NAME in .env)")
    p.add_argument("--stage2-only", action="store_true",
                   help="skip Stage 1, only regenerate criteria/prompts from existing source/")
    p.add_argument("--output-dir", type=Path, default=None,
                   help="write Stage 1 output to this dir (default: source/)")
    p.add_argument("--force", action="store_true",
                   help="overwrite existing files")
    args = p.parse_args()

    global GEN_DIR
    if args.output_dir:
        GEN_DIR = args.output_dir
        GEN_DIR.mkdir(parents=True, exist_ok=True)

    user_name = args.name
    env = load_env()
    api_key = env.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        logging.error("DEEPSEEK_API_KEY not set in state/.env")
        sys.exit(1)

    if not args.stage2_only:
        if args.text:
            documents_text = args.text
        elif args.docs:
            if not args.docs.is_dir():
                logging.error("%s is not a directory", args.docs)
                sys.exit(1)
            documents_text = extract_text(args.docs)
        else:
            logging.error("provide --docs <dir> or --text <...> or --stage2-only")
            sys.exit(1)

        stage1_generate_source(documents_text, user_name, api_key,
                               force=args.force)

    if CHANNELS_FILE.exists():
        stage2_generate_criteria(user_name, api_key, force=args.force)
    else:
        logging.info("no channels.json — skipping Stage 2 (create channel config first)")


if __name__ == "__main__":
    main()
