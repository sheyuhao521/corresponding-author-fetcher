#!/usr/bin/env python3
"""Multi-source corresponding-author literature finder and OA PDF downloader.

Uses public metadata from OpenAlex, Crossref, Europe PMC, and (optionally)
Semantic Scholar.  It never bypasses paywalls: only URLs explicitly exposed as
open-access/full-text PDF locations are downloaded.

Examples:
  python corresponding_author_fetcher.py
  python corresponding_author_fetcher.py --name "Jane Q. Smith" \
      --institution "Example University" --email jane@example.edu \
      --from-year 2019 --to-year 2024
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable, Optional


VERSION = "1.1.0"
USER_AGENT = f"CorrespondingAuthorFetcher/{VERSION} (public-metadata research tool)"
TIMEOUT = 35
RETRIES = 3


def norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def tokens(value: str) -> set[str]:
    stop = {"the", "of", "and", "at", "for", "in", "department", "school",
            "college", "university", "institute", "institution", "hospital"}
    return {x for x in norm(value).split() if len(x) > 1 and x not in stop}


def org_words(value: str) -> list[str]:
    value = html.unescape(value or "")
    words = norm(value).split()
    aliases = {
        "univ": "university", "universite": "university", "universitat": "university",
        "inst": "institute", "institut": "institute", "hosp": "hospital",
        "ctr": "center", "centre": "center", "lab": "laboratory", "labs": "laboratory",
    }
    ignored = {"the", "of", "and", "at", "for", "in", "department", "dept",
               "faculty", "school", "college", "division"}
    return [aliases.get(w, w) for w in words if w not in ignored and len(w) > 1]


def org_acronym(value: str) -> str:
    return "".join(w[0] for w in org_words(value) if w not in {"center", "laboratory"})


def title_key(value: str) -> str:
    return "".join(norm(html.unescape(value)).split())[:240]


def clean_doi(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    value = re.sub(r"^doi:\s*", "", value)
    return value.rstrip(" .")


def safe_filename(value: str, limit: int = 135) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value).strip(" ._")
    return (value[:limit].rstrip(" ._") or "untitled")


def year_from(value: Any) -> Optional[int]:
    if isinstance(value, int):
        return value
    match = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return int(match.group()) if match else None


def name_parts(full_name: str) -> tuple[str, str, list[str]]:
    pieces = norm(full_name).split()
    if not pieces:
        return "", "", []
    return pieces[0], pieces[-1], pieces


def person_matches(target: str, candidate: str) -> bool:
    """Conservative Western-name match supporting 'Family, Given' forms."""
    tf, tl, tp = name_parts(target)
    cf, cl, cp = name_parts(candidate.replace(",", " "))
    if not tp or not cp:
        return False
    # Surname must occur, and a given-name initial must agree. This also handles
    # metadata that reverses names or only supplies initials.
    target_surnames = {tl}
    if "," in target:
        target_surnames.add(norm(target.split(",", 1)[0]))
    if not any(s and s in cp for s in target_surnames):
        return False
    target_given = next((p for p in tp if p not in target_surnames), tf)
    cand_non_surname = [p for p in cp if p not in target_surnames]
    return bool(target_given and any(p[0] == target_given[0] for p in cand_non_surname if p))


def institution_matches(target: str, candidates: Iterable[str]) -> bool:
    wanted_list = org_words(target)
    wanted = set(wanted_list)
    if not wanted_list:
        return True
    target_flat = " ".join(wanted_list)
    target_raw = "".join(norm(target).split())
    target_acronym = org_acronym(target)
    for candidate in candidates:
        got_list = org_words(candidate)
        got = set(got_list)
        if not got:
            continue
        candidate_flat = " ".join(got_list)
        candidate_raw = "".join(norm(html.unescape(candidate)).split())
        candidate_acronym = org_acronym(candidate)
        # Full organization name contained in a departmental affiliation.
        if len(target_raw) >= 6 and target_raw in candidate_raw:
            return True
        if len(candidate_raw) >= 6 and candidate_raw in target_raw:
            return True
        # Common institution acronyms such as MIT and UCSD.
        if 2 <= len(target_raw) <= 8 and target_raw == candidate_acronym:
            return True
        if 2 <= len(candidate_raw) <= 8 and candidate_raw == target_acronym:
            return True
        overlap_count = len(wanted & got)
        coverage = overlap_count / len(wanted)
        # For short names every institution word must agree; for longer names a
        # high target-side coverage is required. A shared city alone cannot pass.
        required = 1.0 if len(wanted) <= 2 else 0.75
        if coverage >= required and overlap_count >= min(2, len(wanted)):
            return True
    return False


def http_request(url: str, *, accept: str = "application/json", api_key: str = "") -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if api_key:
        headers["x-api-key"] = api_key
    request = urllib.request.Request(url, headers=headers)
    last: Optional[Exception] = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last = exc
            code = getattr(exc, "code", 0)
            if code not in (429, 500, 502, 503, 504) or attempt == RETRIES - 1:
                break
            time.sleep(1.5 * (2 ** attempt))
    raise RuntimeError(f"Request failed: {url} ({last})")


def get_json(base: str, params: dict[str, Any], api_key: str = "") -> dict[str, Any]:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    return json.loads(http_request(f"{base}?{query}", api_key=api_key))


@dataclass
class Paper:
    title: str
    year: Optional[int] = None
    doi: str = ""
    pmcid: str = ""
    authors: list[str] = field(default_factory=list)
    affiliations: list[str] = field(default_factory=list)
    matched_author_affiliations: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    landing_urls: list[str] = field(default_factory=list)
    pdf_urls: list[str] = field(default_factory=list)
    identity_evidence: list[str] = field(default_factory=list)
    corresponding_claims: list[str] = field(default_factory=list)
    corresponding_status: str = "uncertain"
    corresponding_evidence: str = "No explicit corresponding-author marker was available"
    downloaded_file: str = ""
    download_error: str = ""

    def key(self) -> str:
        return f"doi:{self.doi}" if self.doi else f"title:{title_key(self.title)}"


def crossref_search(name: str, start: int, end: int, limit: int) -> list[Paper]:
    papers: list[Paper] = []
    cursor = "*"
    while len(papers) < limit:
        data = get_json("https://api.crossref.org/works", {
            "query.author": name,
            "filter": f"from-pub-date:{start}-01-01,until-pub-date:{end}-12-31",
            "select": "DOI,title,author,published,URL,link,license",
            "rows": min(200, limit - len(papers)),
            "cursor": cursor,
        })
        message = data.get("message", {})
        items = message.get("items", [])
        for item in items:
            authors, affils, target_affils = [], [], []
            for author in (item.get("author") or []):
                display = " ".join(x for x in (author.get("given", ""), author.get("family", "")) if x)
                if display:
                    authors.append(display)
                author_affils = [a.get("name", "") for a in (author.get("affiliation") or []) if a.get("name")]
                affils.extend(author_affils)
                if display and person_matches(name, display):
                    target_affils.extend(author_affils)
            date_parts = item.get("published", {}).get("date-parts", [[]])
            y = date_parts[0][0] if date_parts and date_parts[0] else None
            papers.append(Paper(
                title=(item.get("title") or [""])[0], year=year_from(y),
                doi=clean_doi(item.get("DOI", "")), authors=authors,
                affiliations=affils, matched_author_affiliations=target_affils,
                sources=["Crossref"],
                landing_urls=[item.get("URL", "")] if item.get("URL") else [],
            ))
        next_cursor = message.get("next-cursor")
        if not items or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return papers[:limit]


def select_openalex_authors(name: str, institution: str) -> list[str]:
    data = get_json("https://api.openalex.org/authors", {"search": name, "per-page": 25})
    ranked: list[tuple[int, str]] = []
    for item in data.get("results", []):
        if not person_matches(name, item.get("display_name", "")):
            continue
        institutions = [x.get("display_name", "") for x in (item.get("last_known_institutions") or [])]
        score = 2 if institution_matches(institution, institutions) else 0
        score += 1 if norm(item.get("display_name", "")) == norm(name) else 0
        ranked.append((score, item.get("id", "").rsplit("/", 1)[-1]))
    ranked.sort(reverse=True)
    # Keep all institution-consistent identities; fall back to the top name match.
    chosen = [author_id for score, author_id in ranked if score >= 2]
    return chosen or ([ranked[0][1]] if ranked else [])


def openalex_search(name: str, institution: str, start: int, end: int, limit: int) -> list[Paper]:
    papers: list[Paper] = []
    for author_id in select_openalex_authors(name, institution):
        cursor = "*"
        while len(papers) < limit:
            data = get_json("https://api.openalex.org/works", {
                "filter": f"author.id:{author_id},from_publication_date:{start}-01-01,to_publication_date:{end}-12-31",
                "per-page": min(200, limit - len(papers)), "cursor": cursor,
            })
            results = data.get("results", [])
            for item in results:
                authors, affils, target_affils, claims = [], [], [], []
                for authorship in (item.get("authorships") or []):
                    display = authorship.get("author", {}).get("display_name", "")
                    authors.append(display)
                    author_affils = [i.get("display_name", "") for i in (authorship.get("institutions") or [])]
                    author_affils.extend(authorship.get("raw_affiliation_strings") or [])
                    affils.extend(author_affils)
                    if display and person_matches(name, display):
                        target_affils.extend(author_affils)
                        if authorship.get("is_corresponding") is True:
                            claims.append("OpenAlex marks the matched authorship as is_corresponding=true")
                best = item.get("best_oa_location") or {}
                primary = item.get("primary_location") or {}
                pdfs = [u for u in (best.get("pdf_url"), primary.get("pdf_url")) if u]
                landing = [u for u in (primary.get("landing_page_url"), item.get("doi"), item.get("id")) if u]
                ids = item.get("ids") or {}
                papers.append(Paper(
                    title=item.get("display_name", ""), year=year_from(item.get("publication_year")),
                    doi=clean_doi(ids.get("doi", "")), pmcid=(ids.get("pmcid", "").rsplit("/", 1)[-1]),
                    authors=[a for a in authors if a], affiliations=[a for a in affils if a],
                    matched_author_affiliations=[a for a in target_affils if a],
                    corresponding_claims=claims,
                    sources=["OpenAlex"], landing_urls=landing, pdf_urls=pdfs,
                ))
            next_cursor = data.get("meta", {}).get("next_cursor")
            if not results or not next_cursor:
                break
            cursor = next_cursor
    return papers[:limit]


def europe_pmc_search(name: str, start: int, end: int, limit: int) -> list[Paper]:
    papers: list[Paper] = []
    cursor = "*"
    query = f'AUTHOR:"{name.replace(chr(34), "")}" AND FIRST_PDATE:[{start}-01-01 TO {end}-12-31]'
    while len(papers) < limit:
        data = get_json("https://www.ebi.ac.uk/europepmc/webservices/rest/search", {
            "query": query, "format": "json", "resultType": "core",
            "pageSize": min(1000, limit - len(papers)), "cursorMark": cursor,
        })
        results = data.get("resultList", {}).get("result", [])
        for item in results:
            author_objs = (item.get("authorList") or {}).get("author", []) or []
            authors = [a.get("fullName") or " ".join(filter(None, [a.get("firstName"), a.get("lastName")]))
                       for a in author_objs]
            affils: list[str] = []
            target_affils: list[str] = []
            for a in author_objs:
                details = (a.get("authorAffiliationDetailsList") or {}).get("authorAffiliation", []) or []
                author_affils = [x.get("affiliation", "") for x in details if x.get("affiliation")]
                affils.extend(author_affils)
                display = a.get("fullName") or " ".join(filter(None, [a.get("firstName"), a.get("lastName")]))
                if display and person_matches(name, display):
                    target_affils.extend(author_affils)
            pmcid = item.get("pmcid", "")
            pdfs = [f"https://europepmc.org/articles/{pmcid}?pdf=render"] if pmcid else []
            landing = []
            if item.get("doi"):
                landing.append(f"https://doi.org/{clean_doi(item['doi'])}")
            elif pmcid:
                landing.append(f"https://europepmc.org/articles/{pmcid}")
            papers.append(Paper(
                title=item.get("title", ""), year=year_from(item.get("firstPublicationDate") or item.get("pubYear")),
                doi=clean_doi(item.get("doi", "")), pmcid=pmcid,
                authors=[a for a in authors if a], affiliations=affils,
                matched_author_affiliations=target_affils,
                sources=["Europe PMC"], landing_urls=landing, pdf_urls=pdfs,
            ))
        next_cursor = data.get("nextCursorMark")
        if not results or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return papers[:limit]


def semantic_scholar_search(name: str, institution: str, start: int, end: int,
                            limit: int, api_key: str) -> list[Paper]:
    data = get_json("https://api.semanticscholar.org/graph/v1/author/search", {
        "query": name, "limit": 20, "fields": "name,aliases,affiliations,paperCount"
    }, api_key)
    candidates: list[tuple[int, str]] = []
    for item in data.get("data", []):
        names = [item.get("name", "")] + (item.get("aliases") or [])
        if not any(person_matches(name, n) for n in names):
            continue
        score = 2 if institution_matches(institution, item.get("affiliations") or []) else 0
        score += min(int(item.get("paperCount") or 0), 1000) // 1000
        candidates.append((score, item.get("authorId", "")))
    candidates.sort(reverse=True)
    ids = [x[1] for x in candidates if x[0] >= 2] or ([candidates[0][1]] if candidates else [])
    papers: list[Paper] = []
    for author_id in ids:
        offset = 0
        while len(papers) < limit:
            result = get_json(f"https://api.semanticscholar.org/graph/v1/author/{author_id}/papers", {
                "limit": min(100, limit - len(papers)), "offset": offset,
                "fields": "title,year,publicationDate,authors,externalIds,url,openAccessPdf"
            }, api_key)
            batch = result.get("data", [])
            for item in batch:
                y = year_from(item.get("publicationDate") or item.get("year"))
                if not y or not (start <= y <= end):
                    continue
                ext = item.get("externalIds") or {}
                oa = item.get("openAccessPdf") or {}
                papers.append(Paper(
                    title=item.get("title", ""), year=y, doi=clean_doi(ext.get("DOI", "")),
                    pmcid=ext.get("PubMedCentral", ""),
                    authors=[a.get("name", "") for a in item.get("authors", []) if a.get("name")],
                    sources=["Semantic Scholar"], landing_urls=[item.get("url", "")] if item.get("url") else [],
                    pdf_urls=[oa.get("url", "")] if oa.get("url") else [],
                ))
            if not result.get("next") or not batch:
                break
            offset = int(result["next"])
    return papers[:limit]


def merge_papers(items: list[Paper]) -> list[Paper]:
    merged: dict[str, Paper] = {}
    title_aliases: dict[str, str] = {}
    for paper in items:
        if not paper.title:
            continue
        key = paper.key()
        tkey = title_key(paper.title)
        existing_key = key if key in merged else title_aliases.get(tkey, "")
        if not existing_key:
            merged[key] = paper
            title_aliases[tkey] = key
            continue
        old = merged[existing_key]
        if not old.doi and paper.doi:
            old.doi = paper.doi
        if not old.pmcid and paper.pmcid:
            old.pmcid = paper.pmcid
        if not old.year and paper.year:
            old.year = paper.year
        for field_name in ("authors", "affiliations", "matched_author_affiliations", "sources",
                           "landing_urls", "pdf_urls", "corresponding_claims"):
            values = getattr(old, field_name)
            for value in getattr(paper, field_name):
                if value and value not in values:
                    values.append(value)
    return list(merged.values())


def local_identity_filter(papers: list[Paper], name: str, institution: str,
                          start: int, end: int, allow_missing: bool = False
                          ) -> tuple[list[Paper], list[Paper], list[Paper]]:
    selected: list[Paper] = []
    affiliation_mismatch: list[Paper] = []
    affiliation_missing: list[Paper] = []
    for paper in papers:
        if not paper.year or not (start <= paper.year <= end):
            continue
        matching_names = [a for a in paper.authors if person_matches(name, a)]
        if paper.authors and not matching_names:
            continue
        if matching_names:
            paper.identity_evidence.append("author name matched: " + "; ".join(matching_names[:3]))
        else:
            paper.identity_evidence.append("source query matched name; structured author list unavailable")
        # Only affiliations attached to the matched author count. An institution
        # belonging to a co-author must never validate the target identity.
        if paper.matched_author_affiliations:
            if institution_matches(institution, paper.matched_author_affiliations):
                paper.identity_evidence.append("input institution matched the matched author's own affiliation")
            else:
                paper.identity_evidence.append("rejected: matched author's affiliation does not match input institution")
                paper.corresponding_status = "affiliation_mismatch"
                affiliation_mismatch.append(paper)
                continue
        else:
            paper.identity_evidence.append("matched author's affiliation metadata unavailable")
            if not allow_missing:
                paper.corresponding_status = "affiliation_missing"
                affiliation_missing.append(paper)
                continue
        selected.append(paper)
    return selected, affiliation_mismatch, affiliation_missing


def strip_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def node_text(node: ET.Element) -> str:
    return " ".join("".join(node.itertext()).split())


def verify_correspondence_with_jats(paper: Paper, target_name: str, email: str) -> None:
    if not paper.pmcid:
        return
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{paper.pmcid}/fullTextXML"
    try:
        root = ET.fromstring(http_request(url, accept="application/xml,text/xml"))
    except Exception as exc:
        paper.corresponding_evidence = f"PMC full-text correspondence check failed: {exc}"
        return

    corresp_nodes: dict[str, str] = {}
    for node in root.iter():
        if strip_tag(node.tag) == "corresp":
            node_id = node.attrib.get("id", "")
            corresp_nodes[node_id] = node_text(node)

    target_contribs: list[ET.Element] = []
    other_linked = False
    target_linked = False
    for contrib in (n for n in root.iter() if strip_tag(n.tag) == "contrib" and n.attrib.get("contrib-type", "author") == "author"):
        candidate_name = ""
        for child in contrib.iter():
            if strip_tag(child.tag) == "name":
                surname = next((node_text(x) for x in child if strip_tag(x.tag) == "surname"), "")
                given = next((node_text(x) for x in child if strip_tag(x.tag) == "given-names"), "")
                candidate_name = f"{given} {surname}".strip()
                break
        is_target = person_matches(target_name, candidate_name)
        if is_target:
            target_contribs.append(contrib)
        linked = contrib.attrib.get("corresp", "").lower() in ("yes", "true")
        for xref in contrib.iter():
            if strip_tag(xref.tag) == "xref" and xref.attrib.get("ref-type") == "corresp":
                linked = True
        if linked and is_target:
            target_linked = True
        elif linked:
            other_linked = True

    email_norm = email.strip().lower()
    corresp_texts = list(corresp_nodes.values())
    email_in_corresp = bool(email_norm and any(email_norm in text.lower() for text in corresp_texts))
    name_in_corresp = any(person_matches(target_name, text) for text in corresp_texts)
    email_in_target = any(email_norm and email_norm in node_text(c).lower() for c in target_contribs)

    if target_linked or email_in_corresp or (email_in_target and corresp_nodes):
        prior_claims = list(paper.corresponding_claims)
        paper.corresponding_status = "explicit"
        reasons = []
        if target_linked:
            reasons.append("JATS author record links to a correspondence marker")
        if email_in_corresp or email_in_target:
            reasons.append("input email appears in the author's correspondence metadata")
        paper.corresponding_evidence = "; ".join(dict.fromkeys([*prior_claims, *reasons]))
    elif target_contribs and other_linked and paper.corresponding_status != "explicit":
        paper.corresponding_status = "not_corresponding"
        paper.corresponding_evidence = "JATS explicitly marks correspondence, but not for the matched author/email"
    elif corresp_nodes and name_in_corresp:
        paper.corresponding_status = "explicit"
        paper.corresponding_evidence = "matched author name appears in a JATS correspondence block"
    elif paper.corresponding_status != "explicit":
        paper.corresponding_evidence = "PMC full text has no resolvable correspondence marker for the matched author"


def apply_structured_correspondence_claims(paper: Paper) -> None:
    if paper.corresponding_claims:
        paper.corresponding_status = "explicit"
        paper.corresponding_evidence = "; ".join(dict.fromkeys(paper.corresponding_claims))


def fetch_html(url: str, max_bytes: int = 6_000_000) -> tuple[str, str]:
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
    })
    last: Optional[Exception] = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                content_type = response.headers.get("Content-Type", "").lower()
                if "pdf" in content_type:
                    raise RuntimeError("landing URL returned a PDF, not HTML")
                raw = response.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raw = raw[:max_bytes]
                charset = response.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace"), response.geturl()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError) as exc:
            last = exc
            code = getattr(exc, "code", 0)
            if code not in (429, 500, 502, 503, 504) or attempt == RETRIES - 1:
                break
            time.sleep(1.5 * (2 ** attempt))
    raise RuntimeError(f"HTML request failed: {url} ({last})")


def verify_correspondence_with_publisher_page(paper: Paper, target_name: str, email: str) -> None:
    """Use an exact input-email hit near a correspondence label as strong evidence."""
    if paper.corresponding_status == "explicit" or not email.strip():
        return
    doi_url = f"https://doi.org/{paper.doi}" if paper.doi else ""
    candidates = [doi_url] + paper.landing_urls
    checked = 0
    errors: list[str] = []
    email_lower = email.strip().lower()
    surname = name_parts(target_name)[1]
    marker = re.compile(r"correspond(?:ing|ence|ent)?\s*(?:author|authors|address|to)?|corresp(?:onding)?[-_ ]?author",
                        re.IGNORECASE)
    for url in dict.fromkeys(u for u in candidates if u and u.startswith(("http://", "https://"))):
        if checked >= 3:
            break
        if "openalex.org" in url or "semanticscholar.org" in url:
            continue
        checked += 1
        try:
            raw_html, final_url = fetch_html(url)
            text = html.unescape(raw_html)
            lowered = text.lower()
            positions = [m.start() for m in re.finditer(re.escape(email_lower), lowered)]
            for pos in positions:
                context = text[max(0, pos - 1800):pos + 1800]
                # Exact email is the primary identity key; a correspondence label
                # in the same local block prevents ordinary contact/footer emails
                # from being treated as authorship evidence.
                if marker.search(context) and (not surname or surname in norm(context).split()):
                    paper.corresponding_status = "explicit"
                    paper.corresponding_evidence = (
                        f"publisher page contains the exact input email beside a correspondence label: {final_url}"
                    )
                    if final_url not in paper.landing_urls:
                        paper.landing_urls.append(final_url)
                    return
        except Exception as exc:
            errors.append(str(exc))
    if checked and paper.corresponding_status == "uncertain":
        suffix = f" ({len(errors)} page checks failed)" if errors else ""
        paper.corresponding_evidence += f"; publisher-page email/correspondence check found no explicit match{suffix}"


def add_unpaywall_pdf(paper: Paper, api_email: str) -> None:
    if not paper.doi or not api_email:
        return
    try:
        data = get_json(f"https://api.unpaywall.org/v2/{urllib.parse.quote(paper.doi)}", {"email": api_email})
        location = data.get("best_oa_location") or {}
        url = location.get("url_for_pdf")
        if data.get("is_oa") and url and url not in paper.pdf_urls:
            paper.pdf_urls.append(url)
        landing = location.get("url_for_landing_page")
        if landing and landing not in paper.landing_urls:
            paper.landing_urls.append(landing)
    except Exception as exc:
        paper.download_error = f"Unpaywall lookup failed: {exc}"


def download_pdf(paper: Paper, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for url in dict.fromkeys(paper.pdf_urls):
        try:
            data = http_request(url, accept="application/pdf")
            # Some endpoints return an HTML consent/error page with HTTP 200.
            if not data.startswith(b"%PDF-"):
                raise RuntimeError("response is not a PDF")
            ident = paper.doi or paper.pmcid or hashlib.sha1(paper.title.encode("utf-8")).hexdigest()[:10]
            filename = safe_filename(f"{paper.year or 'unknown'}_{ident}_{paper.title}") + ".pdf"
            path = folder / filename
            path.write_bytes(data)
            paper.downloaded_file = str(path.resolve())
            paper.download_error = ""
            return
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    if errors:
        paper.download_error = " | ".join(errors)
    elif not paper.download_error:
        paper.download_error = "No legal open-access PDF URL found"


def original_link(paper: Paper) -> str:
    if paper.doi:
        return f"https://doi.org/{paper.doi}"
    return next(iter(paper.landing_urls), "")


def write_csv(path: Path, papers: list[Paper]) -> None:
    columns = ["year", "title", "doi", "pmcid", "authors", "sources", "status",
               "matched_author_affiliations", "corresponding_evidence", "identity_evidence", "downloaded_file",
               "original_link", "download_error"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for p in sorted(papers, key=lambda x: (x.year or 0, x.title.lower())):
            writer.writerow({
                "year": p.year, "title": p.title, "doi": p.doi, "pmcid": p.pmcid,
                "authors": "; ".join(p.authors), "sources": "; ".join(p.sources),
                "status": p.corresponding_status,
                "matched_author_affiliations": "; ".join(p.matched_author_affiliations),
                "corresponding_evidence": p.corresponding_evidence,
                "identity_evidence": "; ".join(p.identity_evidence),
                "downloaded_file": p.downloaded_file, "original_link": original_link(p),
                "download_error": p.download_error,
            })


def prompt_if_missing(args: argparse.Namespace) -> None:
    # Name and institution identify the person and therefore remain required.
    for attr, label in (("name", "英文名"), ("institution", "作者单位")):
        while getattr(args, attr) in (None, ""):
            setattr(args, attr, input(f"{label}（必填）: ").strip())

    # Email and both date boundaries are optional. Blank dates mean an open
    # boundary, represented internally by 1900 and the current calendar year.
    if args.email is None:
        args.email = input("作者邮箱（可选，直接回车跳过）: ").strip()
    current_year = time.localtime().tm_year
    if args.from_year is None:
        value = input("起始年份（可选，直接回车表示 1900 年）: ").strip()
        args.from_year = int(value) if value else 1900
    if args.to_year is None:
        value = input(f"结束年份（可选，直接回车表示 {current_year} 年）: ").strip()
        args.to_year = int(value) if value else current_year


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检索指定作者作为通讯作者的论文，并下载开放获取 PDF")
    parser.add_argument("--name", help="英文姓名，例如 Jane Q. Smith")
    parser.add_argument("--institution", help="作者单位（英文名称效果最好）")
    parser.add_argument("--email", help="可选；作者邮箱，用于增强通讯作者证据核验")
    parser.add_argument("--from-year", dest="from_year", type=int,
                        help="可选；起始年份（含），交互输入留空默认为 1900")
    parser.add_argument("--to-year", dest="to_year", type=int,
                        help="可选；结束年份（含），交互输入留空默认为当前年份")
    parser.add_argument("--output", default="author_papers", help="输出目录")
    parser.add_argument("--max-per-source", type=int, default=1000, help="每个数据源最多取回条数")
    parser.add_argument("--semantic-scholar-key", default=os.getenv("SEMANTIC_SCHOLAR_API_KEY", ""),
                        help="可选；也可设置 SEMANTIC_SCHOLAR_API_KEY")
    parser.add_argument("--skip-semantic-scholar", action="store_true")
    parser.add_argument("--allow-missing-affiliation", action="store_true",
                        help="允许目标作者缺少单位元数据的记录进入结果（默认严格排除）")
    parser.add_argument("--skip-publisher-page-check", action="store_true",
                        help="不访问 DOI/出版商页面核验通讯邮箱")
    parser.add_argument("--no-download", action="store_true", help="只生成清单，不下载 PDF")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    prompt_if_missing(args)
    if args.from_year > args.to_year:
        raise SystemExit("起始年份不能晚于结束年份")
    # Every execution gets its own directory so PDFs and CSVs from an older,
    # looser query can never be mistaken for current strict-filter results.
    run_stamp = time.strftime("%Y%m%d_%H%M%S")
    run_name = safe_filename(f"{args.name}_{args.from_year}-{args.to_year}_{run_stamp}")
    output = (Path(args.output).expanduser().resolve() / run_name)
    output.mkdir(parents=True, exist_ok=True)

    collectors = [
        ("OpenAlex", lambda: openalex_search(args.name, args.institution, args.from_year, args.to_year, args.max_per_source)),
        ("Crossref", lambda: crossref_search(args.name, args.from_year, args.to_year, args.max_per_source)),
        ("Europe PMC", lambda: europe_pmc_search(args.name, args.from_year, args.to_year, args.max_per_source)),
    ]
    if not args.skip_semantic_scholar:
        collectors.append(("Semantic Scholar", lambda: semantic_scholar_search(
            args.name, args.institution, args.from_year, args.to_year,
            args.max_per_source, args.semantic_scholar_key)))

    all_items: list[Paper] = []
    source_errors: dict[str, str] = {}
    for source, collect in collectors:
        print(f"[{source}] searching...", file=sys.stderr)
        try:
            found = collect()
            all_items.extend(found)
            print(f"[{source}] {len(found)} candidate records", file=sys.stderr)
        except Exception as exc:
            source_errors[source] = str(exc)
            print(f"[{source}] failed: {exc}", file=sys.stderr)

    papers, affiliation_mismatch, affiliation_missing = local_identity_filter(
        merge_papers(all_items), args.name, args.institution, args.from_year, args.to_year,
        allow_missing=args.allow_missing_affiliation)
    print(f"Merged and strict identity-filtered: {len(papers)}; "
          f"affiliation mismatch excluded: {len(affiliation_mismatch)}; "
          f"affiliation missing excluded: {len(affiliation_missing)}", file=sys.stderr)
    for index, paper in enumerate(papers, 1):
        print(f"[{index}/{len(papers)}] verifying: {paper.title[:80]}", file=sys.stderr)
        apply_structured_correspondence_claims(paper)
        verify_correspondence_with_jats(paper, args.name, args.email)
        # Unpaywall requires an email parameter; the user's supplied contact email
        # is used only for this API identification and correspondence matching.
        add_unpaywall_pdf(paper, args.email)
        if not args.skip_publisher_page_check:
            verify_correspondence_with_publisher_page(paper, args.name, args.email)
        if not args.no_download and paper.corresponding_status != "not_corresponding":
            download_pdf(paper, output / paper.corresponding_status / "pdf")

    explicit = [p for p in papers if p.corresponding_status == "explicit"]
    uncertain = [p for p in papers if p.corresponding_status == "uncertain"]
    excluded = [p for p in papers if p.corresponding_status == "not_corresponding"]
    write_csv(output / "明确通讯作者.csv", explicit)
    write_csv(output / "无法确定通讯作者.csv", uncertain)
    write_csv(output / "明确不是通讯作者_已排除.csv", excluded)
    write_csv(output / "单位不符_已排除.csv", affiliation_mismatch)
    write_csv(output / "目标作者单位缺失_已排除.csv", affiliation_missing)
    manifest = {
        "query": {"name": args.name, "institution": args.institution,
                  "email": args.email, "from_year": args.from_year, "to_year": args.to_year},
        "summary": {"explicit": len(explicit), "uncertain": len(uncertain),
                    "excluded_not_corresponding": len(excluded),
                    "excluded_affiliation_mismatch": len(affiliation_mismatch),
                    "excluded_affiliation_missing": len(affiliation_missing),
                    "downloaded": sum(bool(p.downloaded_file) for p in papers)},
        "source_errors": source_errors,
        "papers": [asdict(p) | {"original_link": original_link(p)} for p in papers],
        "affiliation_rejections": [
            asdict(p) | {"original_link": original_link(p)}
            for p in affiliation_mismatch + affiliation_missing
        ],
    }
    (output / "完整结果.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest["summary"], ensure_ascii=False, indent=2))
    print(f"Results: {output}")
    return 0 if papers or not source_errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
