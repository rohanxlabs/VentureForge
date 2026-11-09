from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor,as_completed,TimeoutError
from dataclasses import dataclass,field
import time
from urllib.parse import parse_qsl,urlsplit,urlunsplit,urlencode
from src.research.budget import BudgetExceeded,SearchBudget
from src.research.connectors import ResearchConnectorError
from src.research.models import ResearchQuestion,SourceSnapshot
from src.research.safe_fetch import FetchError,SafeFetcher,UnsafeURLError

HINTS={
    "official":"official government primary sources",
    "research_literature":"research paper journal scientific study",
    "community":"first-hand discussion user experience forum",
    "technical_docs":"official technical documentation API reference",
}
PRIORITY={"high":0,"medium":1,"low":2}

@dataclass
class CollectionFailure:
    question_id:str
    category:str
    connector:str
    reason:str
    error:BaseException|None=field(default=None,repr=False,compare=False)

@dataclass
class SearchAttempt:
    question_id:str
    category:str
    connector:str
    query:str
    status:str
    source_count:int=0
    elapsed_seconds:float=0.0
    error:str=""

@dataclass
class CollectionResult:
    sources:list[SourceSnapshot]=field(default_factory=list)
    failures:list[CollectionFailure]=field(default_factory=list)
    search_calls:int=0
    tavily_credits:int=0
    pages_fetched:int=0
    elapsed_seconds:float=0.0
    searches_attempted:int=0
    searches_succeeded:int=0
    searches_failed:int=0
    attempts:list[SearchAttempt]=field(default_factory=list)

def normalize_source_url(url:str)->str:
    parts=urlsplit(url)
    query=[(k,v) for k,v in parse_qsl(parts.query,keep_blank_values=True) if not k.lower().startswith("utm_") and k.lower() not in {"fbclid","gclid"}]
    host=(parts.hostname or "").lower()
    netloc=host+(f":{parts.port}" if parts.port and parts.port not in {80,443} else "")
    path=parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(),netloc,path,urlencode(sorted(query)), ""))

class SearchOrchestrator:
    def __init__(self,tavily, hackernews, *,fetcher=None, max_workers:int=4):
        if max_workers<1: raise ValueError("At least one search worker is required.")
        self.tavily=tavily; self.hackernews=hackernews
        self.fetcher=fetcher or SafeFetcher(); self.max_workers=max_workers

    def _connectors(self,category:str):
        if category=="community": return [c for c in (self.hackernews,self.tavily) if c is not None]
        if category in HINTS and self.tavily is not None: return [self.tavily]
        return []

    def collect(self,*,run_id:str,prompt:str,questions:list[ResearchQuestion],budget:SearchBudget)->CollectionResult:
        failures=[]; jobs=[]; attempts=[]
        ordered=sorted(enumerate(questions),key=lambda pair:(PRIORITY.get(pair[1].priority.value,9),pair[0]))
        for _,question in ordered:
            for category in dict.fromkeys(question.source_categories):
                if category=="competition_materials":
                    continue
                for connector in self._connectors(category):
                    hint=HINTS.get(category,"")
                    query=" ".join(x for x in (question.text,prompt,hint) if x)
                    try:
                        budget.reserve_search(connector.name,connector.cost_per_search)
                        jobs.append((question,category,connector,query))
                    except BudgetExceeded as exc:
                        failures.append(CollectionFailure(question.id,category,connector.name,str(exc)))
                        attempts.append(SearchAttempt(question.id,category,connector.name,query,
                            "budget_exhausted",error=str(exc)))

        indexed={}; completed=set(); searches_succeeded=0; searches_failed=0
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures={}
            for index,(question,category,connector,query) in enumerate(jobs):
                limit=10 if connector.name=="hackernews" else 3
                started=time.monotonic()
                future=pool.submit(connector.search,run_id=run_id,question_id=question.id,query=query,category=category,max_results=limit)
                futures[future]=(index,question,category,connector,query,started)
            try:
                for future in as_completed(futures,timeout=budget.remaining_seconds()):
                    index,question,category,connector,query,started=futures[future]
                    completed.add(future)
                    try:
                        found=future.result()
                        indexed[index]=found
                        searches_succeeded+=1
                        attempts.append(SearchAttempt(question.id,category,connector.name,query,"succeeded",
                            len(found),round(time.monotonic()-started,3)))
                    except ResearchConnectorError as exc:
                        searches_failed+=1
                        failures.append(CollectionFailure(question.id,category,connector.name,str(exc),exc))
                        attempts.append(SearchAttempt(question.id,category,connector.name,query,"failed",
                            elapsed_seconds=round(time.monotonic()-started,3),error=str(exc)))
            except TimeoutError:
                for future,(index,question,category,connector,query,started) in futures.items():
                    if not future.done():
                        future.cancel()
                        searches_failed+=1
                        error=TimeoutError("Search exceeded the remaining time budget.")
                        failures.append(CollectionFailure(question.id,category,connector.name,str(error),error))
                        attempts.append(SearchAttempt(question.id,category,connector.name,query,"timed_out",
                            elapsed_seconds=round(time.monotonic()-started,3),error=str(error)))
                    elif future not in completed:
                        completed.add(future)
                        try:
                            found=future.result()
                            indexed[index]=found
                            searches_succeeded+=1
                            attempts.append(SearchAttempt(question.id,category,connector.name,query,"succeeded",
                                len(found),round(time.monotonic()-started,3)))
                        except ResearchConnectorError as exc:
                            searches_failed+=1
                            failures.append(CollectionFailure(question.id,category,connector.name,str(exc),exc))
                            attempts.append(SearchAttempt(question.id,category,connector.name,query,"failed",
                                elapsed_seconds=round(time.monotonic()-started,3),error=str(exc)))

        raw_sources=[]
        for index in sorted(indexed): raw_sources.extend(indexed[index])
        sources,fetch_failures=self._fetch_missing(raw_sources,budget)
        failures.extend(fetch_failures)
        merged={}
        for source in sources:
            key=normalize_source_url(source.final_url or source.url)
            if key not in merged:
                merged[key]=source
                continue
            current=merged[key]
            categories=list(dict.fromkeys(current.source_categories+source.source_categories))
            update={"source_categories":categories}
            if not current.content and source.content:
                update.update(content=source.content,content_origin=source.content_origin,
                    content_type=source.content_type,final_url=source.final_url,fetch_error=source.fetch_error)
            merged[key]=current.model_copy(update=update)
        stats=budget.snapshot()
        return CollectionResult(sources=list(merged.values()),failures=failures,
            search_calls=stats["search_calls"],tavily_credits=stats["tavily_credits"],
            pages_fetched=stats["pages_fetched"],elapsed_seconds=stats["elapsed_seconds"],
            searches_attempted=len(jobs),searches_succeeded=searches_succeeded,searches_failed=searches_failed,
            attempts=sorted(attempts,key=lambda item:(item.question_id,item.connector,item.category,item.status)))

    def _fetch_missing(self,sources:list[SourceSnapshot],budget:SearchBudget):
        output=list(sources); failures=[]
        jobs=[]
        for index,source in enumerate(output):
            if source.content.strip() or not source.url: continue
            try: budget.reserve_page()
            except BudgetExceeded as exc:
                output[index]=source.model_copy(update={"fetch_error":str(exc)})
                failures.append(CollectionFailure(source.question_id,"fetch","safe_fetch",str(exc)))
                continue
            jobs.append((index,source))
        if not jobs: return output,failures
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures={pool.submit(self.fetcher.fetch,source.url):(index,source) for index,source in jobs}
            try:
                for future in as_completed(futures,timeout=budget.remaining_seconds()):
                    index,source=futures[future]
                    try:
                        page=future.result()
                        output[index]=source.model_copy(update={"url":page.final_url,"final_url":page.final_url,
                            "content":page.text,"content_type":page.content_type,"content_origin":"safe_fetch","fetch_error":""})
                    except (FetchError,UnsafeURLError) as exc:
                        origin="fetch_blocked" if isinstance(exc,UnsafeURLError) else "fetch_failed"
                        output[index]=source.model_copy(update={"content_origin":origin,"fetch_error":str(exc)})
                        failures.append(CollectionFailure(source.question_id,"fetch","safe_fetch",str(exc)))
            except TimeoutError:
                for future,(index,source) in futures.items():
                    if not future.done():
                        future.cancel()
                        output[index]=source.model_copy(update={"content_origin":"fetch_failed","fetch_error":"Fetch exceeded the remaining time budget."})
                        failures.append(CollectionFailure(source.question_id,"fetch","safe_fetch","Fetch exceeded the remaining time budget."))
        return output,failures
