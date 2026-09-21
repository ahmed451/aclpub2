#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Fallback author resolution for aclpub2's or2papers.py.

Some OpenReview tracks expose the `authors` field (plain names) but restrict
`authorids` (profile IDs) to the track's Program_Chairs.  or2papers.py reads
only `authorids`, so those submissions hit `assert len(authors) > 0`.

This module recovers the author list from `authors` instead:

  1. For each name, search OpenReview for a profile with that exact fullname.
     A single unambiguous hit is resolved through util.get_user, so the author
     record is identical to what the normal path would have produced
     (emails, institution, dblp, orcid, ...).
  2. If there is no hit, or more than one, emit a name-only record built with
     the same first/middle/last splitting rules util.get_user uses.

Every fallback author is written to the log so the result can be audited
before the proceedings are built.  Name-only records are NOT good enough for
Anthology ingestion on their own -- treat them as a list of what still needs
fixing, not as a finished export.
"""

# Surname particles that belong with the last name, not the middle name.
# Mirrors the logic in aclpub2's util.get_user.
_PARTICLES_EXACT = (
    "al", "da", "de", "de la", "del", "dela", "della", "dos", "di", "el",
    "van", "van den", "van der", "von", "von der",
)
# Longest first, so " van den" wins over " van".
_PARTICLES_SUFFIX = (
    " van den", " van der", " von der", " de la", " della", " dela",
    " del", " dos", " van", " von", " al", " da", " de", " di", " el",
)
_SUFFIXES = ("ii", "iii", "iv", "jr", "jr.")


def split_fullname(name):
    """Split a display name into (first, middle, last) the way util.get_user does."""
    name = " ".join(name.split())
    if not name:
        return "", "", ""
    if " " not in name:
        return "", "", name

    parts = name.split(" ")
    first_name = parts[0]
    if len(parts) > 2 and parts[-1].lower() in _SUFFIXES:
        last_name = " ".join(parts[-2:])
        middle_name = " ".join(parts[1:-2])
    else:
        last_name = parts[-1]
        middle_name = " ".join(parts[1:-1])

    lowered = middle_name.lower()
    if lowered in _PARTICLES_EXACT:
        last_name = middle_name + " " + last_name
        middle_name = ""
    else:
        for particle in _PARTICLES_SUFFIX:
            if lowered.endswith(particle):
                last_name = middle_name[-(len(particle) - 1):] + " " + last_name
                middle_name = middle_name[:-len(particle)]
                break

    return first_name, middle_name, last_name


def _titlecase_parts(value):
    """Title-case space-delimited parts that are wholly upper- or lower-case."""
    if len(value) <= 2:
        return value
    return " ".join(
        p.title() if (p == p.upper() or p == p.lower()) else p
        for p in value.split(" ")
    )


def name_only_author(fullname):
    """Build a minimal author record from a display name alone."""
    first_name, middle_name, last_name = split_fullname(fullname)
    first_name = _titlecase_parts(first_name)
    middle_name = _titlecase_parts(middle_name)
    if len(last_name) > 2 and all(
        p.isupper() or p.islower() for p in last_name.split(" ")
    ):
        last_name = last_name.title()

    author = {
        "first_name": first_name,
        "last_name": last_name,
        "name": " ".join(filter(None, [first_name, middle_name, last_name])),
        "username": "",
        "emails": "",
        "institution": "NA",
    }
    if middle_name:
        author["middle_name"] = middle_name
    return author


def _unwrap(value):
    """Strip OpenReview's {'value': ...} wrappers, however deeply nested."""
    seen = 0
    while isinstance(value, dict) and "value" in value and seen < 8:
        value = value["value"]
        seen += 1
    return value


def entry_to_profile_id(entry):
    """Return a '~Profile_Id1' carried by an author entry, if it has one."""
    entry = _unwrap(entry)
    if isinstance(entry, str):
        return entry if entry.startswith("~") else None
    if isinstance(entry, dict):
        for key in ("authorid", "authorids", "id", "username", "profile", "value"):
            candidate = _unwrap(entry.get(key))
            if isinstance(candidate, str) and candidate.startswith("~"):
                return candidate
    return None


def entry_to_name(entry):
    """Return a display name from an author entry, whatever shape it takes.

    Handles plain strings, {'value': 'Name'}, {'fullname': ...} and
    {'first'/'middle'/'last': ...} records.  Returns None if no name can be
    recovered, so the caller can log the entry rather than crash on it.
    """
    entry = _unwrap(entry)

    if isinstance(entry, str):
        name = " ".join(entry.split())
        return name or None

    if isinstance(entry, dict):
        for key in ("fullname", "name", "preferredName", "preferred_name"):
            candidate = _unwrap(entry.get(key))
            if isinstance(candidate, str) and candidate.strip():
                return " ".join(candidate.split())

        parts = []
        for key in ("first", "middle", "last"):
            candidate = _unwrap(entry.get(key))
            if isinstance(candidate, str) and candidate.strip():
                parts.append(candidate.strip())
        if parts:
            return " ".join(" ".join(parts).split())

    return None


def find_profile_id(fullname, client):
    """Return a profile id for `fullname` if exactly one profile matches, else None.

    Matching is deliberately strict: the profile must carry this exact
    fullname (case-insensitive, whitespace-normalised) among its names.
    A near-miss is left unresolved rather than guessed at, because a wrong
    profile means a wrong affiliation and a wrong email in the proceedings.
    """
    target = " ".join(fullname.split()).lower()
    if not target:
        return None

    profiles = []
    for kwargs in ({"fullname": fullname}, {"term": fullname}):
        try:
            profiles = client.search_profiles(**kwargs)
        except Exception:
            profiles = []
        if profiles:
            break

    matches = []
    for profile in profiles:
        content = getattr(profile, "content", None) or {}
        for entry in content.get("names", []):
            candidate = entry.get("fullname")
            if not candidate:
                candidate = " ".join(filter(None, [
                    entry.get("first", ""),
                    entry.get("middle", ""),
                    entry.get("last", ""),
                ]))
            if " ".join(str(candidate).split()).lower() == target:
                matches.append(profile.id)
                break

    unique = sorted(set(matches))
    return unique[0] if len(unique) == 1 else None


def authors_from_names(submission, client, get_user, log=None):
    """Recover an author list for a submission whose `authorids` is not readable.

    Returns (authors, unresolved_count).
    """
    content = submission.content
    entries = _unwrap(content.get("authors"))
    if isinstance(entries, (str, dict)):
        entries = [entries]
    if not entries:
        return [], 0

    def note(message):
        if log is not None:
            log.write(message + "\n")

    number = getattr(submission, "number", "?")
    authors, unresolved = [], 0

    # Record the raw shape once per submission so odd inputs are auditable.
    note(f"#{number}: raw authors entry -> {entries[0]!r}")

    for entry in entries:
        # If the entry itself carries a profile id, the normal path still works.
        embedded_id = entry_to_profile_id(entry)
        if embedded_id:
            author, error = get_user(embedded_id, client)
            if not error:
                note(f"#{number}: used profile id {embedded_id} embedded in the authors field")
                authors.append(author)
                continue

        fullname = entry_to_name(entry)
        if not fullname:
            unresolved += 1
            note(f"#{number}: UNREADABLE author entry {entry!r} -- skipped")
            continue

        profile_id = find_profile_id(fullname, client)
        if profile_id:
            author, error = get_user(profile_id, client)
            if not error:
                note(f"#{number}: resolved {fullname!r} -> {profile_id} by name search")
                authors.append(author)
                continue
            note(f"#{number}: {profile_id} (matched from {fullname!r}) failed to load")
        unresolved += 1
        note(f"#{number}: NAME ONLY for {fullname!r} -- no unique profile match; "
             f"email and affiliation are missing")
        authors.append(name_only_author(fullname))

    return authors, unresolved