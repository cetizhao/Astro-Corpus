"""Bounded, serial online HTML evidence jobs for the existing library viewer."""
import copy
import hashlib
import json
import threading
import re
import time
import unicodedata

import httpx

from astro_pipeline.paper_evidence import analyze_paper, _matches
from astro_pipeline.sources.paper_fulltext import ArxivClient


def title_key(title):
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", title).casefold()))


def resolve_online_paper(paper):
    """Return verified identifiers or explicit uncertainty, never inferred absence."""
    from astro_pipeline.sources.simbad_refs import arxiv_id_from_doi
    from astro_pipeline.sources.arxiv_search import search_arxiv
    if paper.get("arxiv_id"):
        return {"id": paper["arxiv_id"], "method": "existing_identifier"}
    errors = []
    if paper.get("doi"):
        try:
            aid = arxiv_id_from_doi(paper["doi"], expected_titles=[paper.get("title") or ""])
            if aid:
                return {"id": aid, "method": "doi"}
        except Exception as exc:
            errors.append(str(exc))
            if "429" in str(exc):
                return {"status": "rate_limited", "errors": errors}
    title = paper.get("title") or ""
    if not title.strip():
        return {"status": "insufficient_metadata", "errors": errors}
    try:
        time.sleep(3)
        words = [w for w in re.findall(r"[^\W_]+", title, re.UNICODE)
                 if len(w) > 3 and w.lower() not in {"with", "from", "onto", "into", "around", "nearby", "first", "their", "that", "this"}]
        query = " AND ".join('ti:"' + word + '"' for word in words[:4])
        candidates = search_arxiv(query, max_results=10, max_retries=2)
        exact = [p for p in candidates if title_key(p.title) == title_key(title)]
        ids = {p.arxiv_id for p in exact}
        if len(ids) == 1:
            return {"id": next(iter(ids)), "method": "normalized_exact_title",
                    "abstract": exact[0].abstract}
        return {"status": "identifier_candidates" if candidates else "identifier_unresolved",
                "candidates": [{"arxiv_id": p.arxiv_id, "title": p.title} for p in candidates],
                "errors": errors}
    except Exception as exc:
        return {"status": "rate_limited" if "429" in str(exc) else "identifier_lookup_failed", "errors": errors + [str(exc)]}


def discover(source_id, release):
    if release not in {"dr3", "dr4"} or not source_id.isdecimal():
        raise ValueError("Invalid release/source identity")
    identifier = f"Gaia {release.upper()} {source_id}"
    def query(sql):
        with httpx.Client(timeout=45, follow_redirects=True) as client:
            r = client.post("https://simbad.cds.unistra.fr/simbad/sim-tap/sync",
                data={"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "json", "QUERY": sql})
            r.raise_for_status()
            data = r.json()
        return [dict(zip([c["name"] for c in data["metadata"]], row)) for row in data["data"]]
    aliases = query(f"SELECT ident.id FROM ident JOIN ident AS target ON ident.oidref=target.oidref WHERE target.id='{identifier}'")
    papers = query(f"SELECT ref.bibcode, ref.title, ref.\"year\", ref.doi, ref.abstract FROM ref JOIN has_ref ON ref.oidbib=has_ref.oidbibref JOIN ident ON ident.oidref=has_ref.oidref WHERE ident.id='{identifier}'")
    names = {identifier}
    for row in aliases:
        name = row["id"]
        if name:
            names.add(name)
            for prefix in ("V* ", "* "):
                if name.startswith(prefix):
                    names.add(name[len(prefix):])
    return sorted(names), list({r["bibcode"]: r for r in papers}.values())


class OnlineEvidence:
    """One active worker per viewer; reads never start downloads or write SQLite."""
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.jobs = {}
        self.active = None
        self.retry_at = 0

    def status(self, release, source_id):
        with self.lock:
            job = self.jobs.get((release, source_id), {"state": "idle"})
            return copy.deepcopy({k:v for k,v in job.items() if k != "discovered"})

    def start(self, release, source_id, cached_papers, cancel=False, bibcodes=None):
        if release not in {"dr3", "dr4"} or not source_id.isdecimal():
            raise ValueError("Invalid release/source identity")
        key = (release, source_id)
        with self.lock:
            if cancel:
                if key in self.jobs:
                    self.jobs[key]["cancel_requested"] = True
                return self.status(*key)
            if self.active:
                raise ValueError("已有检索任务运行，请等待完成或停止后再提交")
            if time.time() < self.retry_at:
                raise ValueError(f"arXiv限流冷却中，请在{int(self.retry_at-time.time())+1}秒后重试")
            if bibcodes is not None:
                known = {p.get("bibcode") for p in cached_papers}
                if not isinstance(bibcodes, list) or not bibcodes or any(not isinstance(b, str) or b not in known for b in bibcodes):
                    raise ValueError("请选择当前源列表中的论文")
            job = self.jobs.setdefault(key, {"state": "pending", "papers": [], "processed": 0, "aliases": [], "cancel_requested": False})
            job.update(state="running", cancel_requested=False, error=None, requested=bibcodes, processed=0, rate_limited=False)
            self.active = key
            threading.Thread(target=self._run, args=(key, cached_papers), daemon=True).start()
            return self.status(*key)

    def _run(self, key, cached):
        from astro_pipeline.sources.simbad_refs import bibcode_to_arxiv, arxiv_id_from_doi
        release, sid = key
        job = self.jobs[key]
        try:
            if not job.get("discovered"):
                aliases, papers = discover(sid, release)
                # Shared bibliography metadata must benefit from live discovery,
                # not remain stuck at imported bibcode-only placeholders.
                if self.store:
                    for metadata in papers:
                        self.store.upsert_paper(metadata)
                local = {p.get("bibcode"): p for p in cached}
                for p in papers:
                    p["arxiv_id"] = local.get(p["bibcode"], {}).get("arxiv_id") or bibcode_to_arxiv(p["bibcode"])
                    p["title_match"] = _matches(p.get("title") or "", aliases)
                papers.sort(key=lambda p: (bool(p["title_match"]), p.get("year") or 0), reverse=True)
                with self.lock:
                    job.update(aliases=aliases, discovered=papers, total=len(papers), title_matches=[p for p in papers if p["title_match"]])
            requested = job.get("requested")
            if requested is None:
                queue = job["discovered"][:10]
            else:
                discovered = {p["bibcode"]: p for p in job["discovered"]}
                local = {p["bibcode"]: p for p in cached}
                queue = [discovered.get(b) or local[b] for b in dict.fromkeys(requested)]
            with self.lock:
                job["total"] = len(queue)
            with ArxivClient() as client:
                for paper in queue:
                    if job["cancel_requested"]:
                        break
                    with self.lock:
                        job["current"] = paper.get("title") or paper["bibcode"]
                        job["current_bibcode"] = paper["bibcode"]
                    item = {"paper": paper, "status": "pending", "evidence": []}
                    try:
                        resolution = resolve_online_paper(paper)
                        item["resolution"] = resolution
                        aid = resolution.get("id")
                        if not aid:
                            item["status"] = resolution["status"]
                        else:
                            meta = client.resolve(aid)
                            version = meta["versioned_id"]
                            cache_key = hashlib.sha256(json.dumps([version, release, sid, job["aliases"], "html-evidence-2-vizier"]).encode()).hexdigest()
                            previous = self.store.get_cache("online-paper-evidence", cache_key) if self.store else None
                            if previous:
                                item = previous["payload"]
                            else:
                                artifact = client.fetch(version, "html")
                                result = analyze_paper(source_id=sid, release=release, aliases=job["aliases"],
                                    bibcode=paper["bibcode"], html=artifact.data.decode("utf-8"),
                                    document_url=artifact.final_url, paper_version=version,
                                    alias_provenance="SIMBAD live ident lookup")
                                result["document"].pop("original_html", None)
                                item = {"paper": paper, "status": result["status"], "evidence": result["evidence"],
                                        "resolution": resolution,
                                        "abstract": result["paper"]["abstract"], "groups": result["groups"],
                                        "document": result["document"], "coverage": result["coverage"]}
                                item["aliases_checked"] = job["aliases"]
                                if not result["evidence"]:
                                    from astro_pipeline.sources.vizier_evidence import find_vizier_evidence
                                    with self.lock:
                                        job["current"] = "检查VizieR关联表：" + (paper.get("title") or paper["bibcode"])
                                    fallback = find_vizier_evidence(paper["bibcode"], job["aliases"])
                                    item["vizier"] = fallback
                                    item["evidence"].extend(fallback["evidence"])
                                    if fallback["evidence"]:
                                        item["groups"].append("vizier")
                                        item["status"] = "mentions_found"
                                if self.store:
                                    with self.store.hold_lease("online-paper-evidence", cache_key,
                                            ttl=self.store.lease_seconds, wait_timeout=self.store.wait_timeout_seconds) as lease:
                                        self.store.put_cache("online-paper-evidence", cache_key, item, lease=lease)
                    except Exception as exc:
                        item.update(status="rate_limited" if "429" in str(exc) else "html_unavailable_or_failed", error=str(exc))
                    with self.lock:
                        job["papers"] = [p for p in job["papers"] if p["paper"]["bibcode"] != paper["bibcode"]] + [item]
                        job["processed"] += 1
                        if item["status"] == "rate_limited":
                            self.retry_at = time.time() + 120
                            job.update(rate_limited=True, retry_at=self.retry_at,
                                       error="arXiv限流，已停止本批后续请求；至少等待2分钟后可重新检索未完成项")
                            break
            with self.lock:
                job["state"] = "paused" if job.get("rate_limited") else "cancelled" if job["cancel_requested"] else "completed" if job["processed"] == job["total"] else "paused"
        except Exception as exc:
            with self.lock:
                job.update(state="failed", error=str(exc))
        finally:
            with self.lock:
                job["current"] = None
                job["current_bibcode"] = None
                self.active = None
