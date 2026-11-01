from __future__ import annotations
import time
from typing import Any
from uuid import uuid4
import diskcache
import requests
from src.config import settings
from src.research.models import SourceSnapshot

_CACHE=diskcache.Cache(settings.cache_dir)
_TTL_SECONDS=settings.cache_ttl_hours*3600
_RETRY_STATUSES={408,425,429,500,502,503,504}

def request_json(session:Any,method:str,url:str,*,timeout:int,payload:dict|None=None,params:dict|None=None,retries:int=2,sleep_fn=time.sleep)->dict:
    for attempt in range(retries+1):
        try:
            response=getattr(session,method)(url,json=payload,params=params,timeout=timeout)
            status=getattr(response,"status_code",200)
            if status in _RETRY_STATUSES and attempt<retries:
                retry_after=getattr(response,"headers",{}).get("Retry-After")
                try: delay=min(float(retry_after),4.0) if retry_after else min(0.5*(2**attempt),4.0)
                except ValueError: delay=min(0.5*(2**attempt),4.0)
                sleep_fn(delay)
                continue
            response.raise_for_status()
            return response.json()
        except requests.HTTPError:
            raise
        except requests.RequestException:
            if attempt>=retries: raise
            sleep_fn(min(0.5*(2**attempt),4.0))
    raise RuntimeError("Request retry loop ended unexpectedly.")

def _restore_sources(rows:list[dict],run_id:str,question_id:str,category:str)->list[SourceSnapshot]:
    out=[]
    for row in rows:
        data=dict(row); data["id"]=str(uuid4()); data["run_id"]=run_id; data["question_id"]=question_id
        data["source_categories"]=[category]
        out.append(SourceSnapshot.model_validate(data))
    return out

class TavilyConnector:
    name="tavily"
    cost_per_search=1
    def __init__(self,api_key:str,*,timeout:int=20,session:Any=None,cache:Any=None,retries:int=2,sleep_fn=time.sleep):
        if not api_key: raise ValueError("TAVILY_API_KEY is required.")
        self.key=api_key; self.timeout=timeout; self.session=session or requests
        self.cache=cache if cache is not None else _CACHE; self.retries=retries; self.sleep_fn=sleep_fn
    def search(self,*,run_id:str,question_id:str,query:str,category:str="web",max_results:int=3)->list[SourceSnapshot]:
        cache_key=("research_tavily",query,category,max_results)
        cached=self.cache.get(cache_key,default=None)
        if cached is not None: return _restore_sources(cached,run_id,question_id,category)
        payload={"api_key":self.key,"query":query,"search_depth":"basic","max_results":max_results,"include_answer":False,"include_raw_content":True}
        data=request_json(self.session,"post","https://api.tavily.com/search",timeout=self.timeout,payload=payload,retries=self.retries,sleep_fn=self.sleep_fn)
        rows=[]; seen=set()
        for item in data.get("results",[]):
            url=str(item.get("url","")).strip()
            if not url or url in seen: continue
            seen.add(url)
            raw=str(item.get("raw_content") or "")
            rows.append(SourceSnapshot(run_id=run_id,question_id=question_id,url=url,title=str(item.get("title","")),publisher=str(item.get("source","")),published_at=item.get("published_date"),content=raw,search_snippet=str(item.get("content") or ""),search_score=item.get("score"),source_categories=[category],content_origin="provider_raw_content" if raw else "search_result_only"))
        self.cache.set(cache_key,[x.model_dump(mode="json") for x in rows],expire=_TTL_SECONDS)
        return rows

class HackerNewsConnector:
    name="hackernews"
    cost_per_search=0
    def __init__(self,*,timeout:int=15,session:Any=None,cache:Any=None,retries:int=2,sleep_fn=time.sleep):
        self.timeout=timeout; self.session=session or requests
        self.cache=cache if cache is not None else _CACHE; self.retries=retries; self.sleep_fn=sleep_fn
    def search(self,*,run_id:str,question_id:str,query:str,category:str="community",max_results:int=10)->list[SourceSnapshot]:
        cache_key=("research_hn",query,max_results)
        cached=self.cache.get(cache_key,default=None)
        if cached is not None: return _restore_sources(cached,run_id,question_id,category)
        data=request_json(self.session,"get","https://hn.algolia.com/api/v1/search_by_date",timeout=self.timeout,
            params={"query":query,"tags":"comment","hitsPerPage":min(max_results,50)},retries=self.retries,sleep_fn=self.sleep_fn)
        import re
        from html import unescape
        rows=[]; seen=set()
        for hit in data.get("hits",[]):
            text=unescape(re.sub(r"<[^>]+>"," ",str(hit.get("comment_text") or "")))
            text=" ".join(text.split())
            object_id=str(hit.get("objectID") or "")
            if not text or not object_id or object_id in seen: continue
            seen.add(object_id)
            rows.append(SourceSnapshot(run_id=run_id,question_id=question_id,url=f"https://news.ycombinator.com/item?id={object_id}",
                title=str(hit.get("story_title") or "Hacker News comment"),publisher="Hacker News",
                published_at=hit.get("created_at"),content=text,source_categories=[category],content_origin="api_content"))
        self.cache.set(cache_key,[x.model_dump(mode="json") for x in rows],expire=_TTL_SECONDS)
        return rows
