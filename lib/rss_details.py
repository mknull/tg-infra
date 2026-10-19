"""Small, verified detail extractors for publishers whose feeds contain summaries."""

import hashlib
import json
import os
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit


class _Details(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.core, self.scripts = [], []
        self.depth = 0
        self.script = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self.script = []
        if tag == "div" and (self.depth or attrs.get("id") == "content-core"):
            self.depth += 1
        if self.depth:
            self.core.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.scripts.append("".join(self.script))
            self.script = None
        if self.depth:
            self.core.append(f"</{tag}>")
            if tag == "div":
                self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.core.append(data)
        if self.script is not None:
            self.script.append(data)

    def handle_entityref(self, name):
        self.handle_data(f"&{name};")

    def handle_charref(self, name):
        self.handle_data(f"&#{name};")


def _jobs(value):
    if isinstance(value, list):
        for item in value:
            yield from _jobs(item)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        if types == "JobPosting" or isinstance(types, list) and "JobPosting" in types:
            yield value
        yield from _jobs(value.get("@graph", []))


def extract_detail(raw: bytes, parser: str, url: str, plain_text) -> str:
    document = _Details()
    document.feed(raw.decode("utf-8"))
    document.close()
    if parser == "fsf":
        content = plain_text("".join(document.core), url)
    elif parser == "nodesk":
        jobs = []
        for script in document.scripts:
            try:
                jobs.extend(_jobs(json.loads(script)))
            except ValueError:
                continue
        if len(jobs) != 1 or not isinstance(jobs[0].get("description"), str):
            raise ValueError("NoDesk page needs one JobPosting description")
        job = jobs[0]
        content = plain_text(job["description"], url)
        for field in ("employmentType", "jobLocation", "jobLocationType", "applicantLocationRequirements", "baseSalary"):
            if job.get(field):
                content += f"\n{field}: " + json.dumps(job[field], ensure_ascii=False)
    else:
        raise ValueError("Unknown detail extractor")
    if len(content.strip()) < 120:
        raise ValueError(f"{parser}: full job description missing or too short")
    return content


def enrich_item(item: dict, parser: str, cache: Path, fetch, plain_text) -> dict:
    url = item["url"]
    hosts = {"fsf": {"fsf.org", "www.fsf.org"}, "nodesk": {"nodesk.co"}}
    if urlsplit(url).hostname not in hosts[parser]:
        raise ValueError(f"{parser}: unexpected job-page host")
    if parser == "fsf":
        url = url.replace("http://", "https://", 1)
    digest = hashlib.sha256((parser + "\0" + url + "\0" + item["content_hash"]).encode()).hexdigest()
    path = cache / (digest + ".txt")
    if path.exists():
        description = path.read_text()
    else:
        raw, response = fetch(url, {})
        if raw is None or urlsplit(response.get("response_url", url)).hostname not in hosts[parser]:
            raise ValueError(f"{parser}: invalid detail-page response")
        description = extract_detail(raw, parser, url, plain_text)
        cache.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".detail-", dir=cache)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(description)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    content = item["content"] + "\n\nFull description:\n" + description
    return {**item, "content": content, "content_hash": hashlib.sha256(content.encode()).hexdigest()}
