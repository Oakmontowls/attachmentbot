from rapidfuzz import fuzz
import re

def normalize_text(text: str) -> str:
    text = text.lower()

    replacements = {
        "0": "o",
        "1": "l",
        "5": "s",
        "$": "s",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    text = re.sub(r"[^a-z0-9\s\.]", " ", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def fuzzy_alias_match(text: str, alias: str) -> bool:
    if alias in text:
        return True

    words = text.split()
    alias_words = alias.split()

    window = len(alias_words)

    for i in range(len(words)):
        chunk = " ".join(words[i:i + window])

        if fuzz.ratio(chunk, alias) >= 95:
            return True

    return False


def extract_keywords(text: str, KEYWORDS: dict):
    text = normalize_text(text)
    found = {}

    for keyword, data in KEYWORDS.items():
        aliases = [keyword] + data.get("aliases", [])

        for alias in aliases:
            if fuzzy_alias_match(text, alias):
                found[keyword] = data["points"]
                break

    return found


def score(text: str, KEYWORDS: dict):
    hits = extract_keywords(text, KEYWORDS)
    return sum(hits.values()), hits